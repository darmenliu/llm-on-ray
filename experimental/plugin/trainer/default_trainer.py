import os
import math
import time

import torch
import transformers

from ray.air.checkpoint import Checkpoint

from .. import dataprocesser
from .trainer import Trainer

from ..logging import logger

class DefaultTrainer(Trainer):
    def __init__(self, config):
        self.config = config
        dataprocesser_config = config.get("dataprocesser")
        dataprocesser_type = dataprocesser_config.get("type")
        Factory = dataprocesser.DataProcesser.registory.get(dataprocesser_type)
        if Factory is None:
            raise ValueError(f"there is no {dataprocesser_type} dataprocesser.")
        self.dataprocesser = Factory(dataprocesser_config)
        self.starting_epoch = 0

    def recovery(self, config):
        if config is None or config is {}:
            logger.warning(f"checkpoint is empty, skip")
        root_path = config.get("root_path")
        model_name = config.get("model_name", "")
        if root_path is None:
            logger.warning(f"checkpoint root_path is empty, skip")
        local_checkpoint_path = self._get_local_path(root_path, model_name)
        try:
            logger.info(f"start recovery from {local_checkpoint_path}")
            checkpoint_dict = Checkpoint.from_directory(local_checkpoint_path).to_dict()
            model_state = checkpoint_dict["model"]
            self.model.load_state_dict(model_state)
            # update optimizer status
            optimizer_state = checkpoint_dict["optimizer_state_dict"]
            self.optimizer.load_state_dict(optimizer_state)
            # update lr_scheduler status
            if "lr_scheduler" in checkpoint_dict and hasattr(self, "lr_scheduler"):
                scheduler_state = checkpoint_dict["lr_scheduler"]
                self.lr_scheduler.load_state_dict(scheduler_state)
            # update current epoch
            checkpoint_epoch = checkpoint_dict["epoch"]
            self.starting_epoch = checkpoint_epoch + 1
            logger.info(f"recovery to epoch {self.starting_epoch}")
        except Exception as e:
            logger.warning(f"recovery error", exc_info=True)

    def _coordinate(self, accelerator):
        self.accelerator = accelerator
        self.rank = accelerator.process_index
        self.size = accelerator.num_processes
        self.local_rank = accelerator.local_process_index
        accelerator.wait_for_everyone()
        logger.info(f"coordinate workers finish, cluster size:{self.size} worker rank:{self.rank} worker local_rank:{self.local_rank}")

    def _get_lr_scheduler(self, lr_scheduler_config, optimizer, num_train_epochs, num_steps_per_epoch, accelerator):
        # gradient_accumulation_steps = accelerator.gradient_accumulation_steps
        # num_update_steps_per_epoch = math.ceil(num_steps_per_epoch / gradient_accumulation_steps)
        enable = lr_scheduler_config.get("enable", False)
        if not enable:
            return None
        max_train_steps  = lr_scheduler_config.get("max_train_steps")
        lr_scheduler_type = lr_scheduler_config.get("lr_scheduler_type", "linear")
        num_warmup_steps = lr_scheduler_config.get("num_warmup_steps", 0)

        if max_train_steps is None:
            max_train_steps = num_steps_per_epoch * num_train_epochs

        lr_scheduler = transformers.get_scheduler(
            name=lr_scheduler_type,
            optimizer=optimizer,
            num_warmup_steps=num_warmup_steps,
            num_training_steps=max_train_steps,
        )
        return lr_scheduler

    def prepare(self, model, tokenizer, dataset, optimizer, accelerator):
        self._coordinate(accelerator)

        embedding_size = model.get_input_embeddings().weight.shape[0]
        logger.info(f"model embedding size: {embedding_size}")
        if len(tokenizer) > embedding_size:
            model.resize_token_embeddings(len(tokenizer))
            logger.warning(f"model embedding size resize to {len(tokenizer)} because of tokenizer size")

        train_dataloader, eval_dataloader = self.dataprocesser.prepare(
            tokenizer, dataset
        )

        lr_scheduler_config = self.config.get("lr_scheduler")
        if lr_scheduler_config:
            num_steps_per_epoch = len(train_dataloader)
            num_train_epochs = self.config.get("num_train_epochs", 1)
            lr_scheduler = self._get_lr_scheduler(lr_scheduler_config, optimizer, num_train_epochs, num_steps_per_epoch, accelerator)
        else:
            lr_scheduler = None

        self.model, self.optimizer, self.lr_scheduler = accelerator.prepare(
            model, optimizer, lr_scheduler
        )

        self.train_dataloader, self.eval_dataloader = accelerator.prepare(
            train_dataloader, eval_dataloader,
        )

        checkpoint = self.config.get("checkpoint")
        if checkpoint is not None:
            self.recovery(checkpoint)

    def train(self):
        num_train_epochs = self.config.get("num_train_epochs", 1)
        checkpoint = self.config.get("checkpoint")
        log_step = self.config.get("log_step", 1)
        for idx in range(self.starting_epoch, num_train_epochs, 1):
            logger.info(f"start train epoch {idx}")
            self.model.train()
            start = time.time()
            for step, batch in enumerate(self.train_dataloader):
                with self.accelerator.accumulate(self.model):
                    outputs = self.model(**batch)
                    loss = outputs.loss
                    self.accelerator.backward(loss)
                    self.optimizer.step()
                    if self.lr_scheduler is not None:
                        self.lr_scheduler.step()
                    self.optimizer.zero_grad()
                    if step % log_step == 0:
                        logger.info(f"train epoch:[{idx}/{num_train_epochs}]\tstep:[{step}/{len(self.train_dataloader)}]\tloss:{loss}\tppl:{math.exp(loss)}\ttime:{time.time()-start}")
                        start = time.time()
                if step == 0:
                    break

            if self.eval_dataloader:
                logger.info(f"start eval epoch {idx}")
                self.model.eval()
                start = time.time()
                losses = []
                for step, batch in enumerate(self.eval_dataloader):
                    with torch.no_grad():
                        outputs = self.model(**batch)
                    loss = outputs.loss
                    losses.append(self.accelerator.gather_for_metrics(loss.repeat(2)))
                    if step == 0:
                        break

                losses = torch.cat(losses)
                try:
                    eval_loss = torch.mean(losses)
                    perplexity = math.exp(eval_loss)
                except OverflowError:
                    eval_loss = float("inf")
                    perplexity = float("inf")
                logger.info(f"eval epoch:[{idx}/{num_train_epochs}]\tloss:[{eval_loss}]\tppl:[{perplexity}]\ttime:[{time.time()-start}]")

            if checkpoint is not None:
                self.save(checkpoint, idx)
            self.accelerator.wait_for_everyone()

        output = self.config.get("output", "./output")
        if output is not None:
            logger.info(f"start save model to {output}")
            unwrapped_model = self.accelerator.unwrap_model(self.model)
            unwrapped_model.save_pretrained(
                output, is_main_process=self.accelerator.is_main_process, save_function=self.accelerator.save
            )
            logger.info(f"finish save model to {output}")
        self.accelerator.wait_for_everyone()

    def _get_local_path(self, root_path, model_name):
        return f"{root_path}/{model_name}_{self.rank}-of-{self.size}"

    def save(self, config, epoch = 0):
        if config is None or config is {}:
            logger.warning(f"checkpoint is empty, skip")
        root_path = config.get("root_path")
        model_name = config.get("model_name", "")
        if root_path is None:
            logger.warning(f"checkpoint root_path is empty, skip")
        local_checkpoint_path = self._get_local_path(root_path, model_name)

        logger.info(f"save checkpoint to {local_checkpoint_path}")
        unwrapped_model = self.accelerator.unwrap_model(self.model)
        status = {
            "epoch": epoch,
            "model": unwrapped_model.state_dict(),
            "optimizer_state_dict": self.optimizer.state_dict(),
        }
        if self.lr_scheduler:
            status["lr_scheduler"] = self.lr_scheduler.state_dict()
        checkpoint = Checkpoint.from_dict(status)
        Checkpoint.to_directory(checkpoint, local_checkpoint_path)
        logger.info(f"save checkpoint finish")

