# Standard
from datetime import datetime
from typing import Optional, Union
import json
import os

# Third Party
from peft.utils.other import fsdp_auto_wrap_policy
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    GPT2Tokenizer,
    GPTNeoXTokenizerFast,
    LlamaTokenizer,
    LlamaTokenizerFast,
    TrainerCallback,
)
from transformers.utils import logging
from trl import DataCollatorForCompletionOnlyLM, SFTTrainer
import datasets
import fire
import transformers

# Local
from tuning.aim_loader import get_aimstack_callback
from tuning.config import configs, peft_config
from tuning.data import tokenizer_data_utils
from tuning.utils.config_utils import get_hf_peft_config
from tuning.utils.data_type_utils import get_torch_dtype


class PeftSavingCallback(TrainerCallback):
    def on_save(self, args, state, control, **kwargs):
        checkpoint_path = os.path.join(
            args.output_dir, f"checkpoint-{state.global_step}"
        )
        kwargs["model"].save_pretrained(checkpoint_path)

        if "pytorch_model.bin" in os.listdir(checkpoint_path):
            os.remove(os.path.join(checkpoint_path, "pytorch_model.bin"))


class FileLoggingCallback(TrainerCallback):
    """Exports metrics, e.g., training loss to a file in the checkpoint directory."""

    def __init__(self, logger):
        self.logger = logger

    def on_log(self, args, state, control, logs=None, **kwargs):
        """Checks if this log contains keys of interest, e.g., loss, and if so, creates
        train_loss.jsonl in the model output dir (if it doesn't already exist),
        appends the subdict of the log & dumps the file.
        """
        # All processes get the logs from this node; only update from process 0.
        if not state.is_world_process_zero:
            return

        # separate evaluation loss with train loss
        log_file_path = os.path.join(args.output_dir, "train_loss.jsonl")
        eval_log_file_path = os.path.join(args.output_dir, "eval_loss.jsonl")
        if logs is not None and "loss" in logs and "epoch" in logs:
            self._track_loss("loss", log_file_path, logs, state)
        elif logs is not None and "eval_loss" in logs and "epoch" in logs:
            self._track_loss("eval_loss", eval_log_file_path, logs, state)

    def _track_loss(self, loss_key, log_file, logs, state):
        try:
            # Take the subdict of the last log line; if any log_keys aren't part of this log
            # object, assume this line is something else, e.g., train completion, and skip.
            log_obj = {
                "name": loss_key,
                "data": {
                    "epoch": round(logs["epoch"], 2),
                    "step": state.global_step,
                    "value": logs[loss_key],
                    "timestamp": datetime.isoformat(datetime.now()),
                },
            }
        except KeyError:
            return

        # append the current log to the jsonl file
        with open(log_file, "a") as f:
            f.write(f"{json.dumps(log_obj, sort_keys=True)}\n")


def train(
    model_args: configs.ModelArguments,
    data_args: configs.DataArguments,
    train_args: configs.TrainingArguments,
    peft_config: Optional[
        Union[peft_config.LoraConfig, peft_config.PromptTuningConfig]
    ] = None,
):
    """Call the SFTTrainer

    Args:
        model_args: tuning.config.configs.ModelArguments
        data_args: tuning.config.configs.DataArguments
        train_args: tuning.config.configs.TrainingArguments
        peft_config: peft_config.LoraConfig for Lora tuning | \
        peft_config.PromptTuningConfig for prompt tuning | \
        None for fine tuning
            The peft configuration to pass to trainer
    """
    run_distributed = int(os.environ.get("WORLD_SIZE", "1")) > 1

    logger = logging.get_logger("sft_trainer")

    # Validate parameters
    if (not isinstance(train_args.num_train_epochs, float)) or (
        train_args.num_train_epochs <= 0
    ):
        raise ValueError("num_train_epochs has to be an integer/float >= 1")
    if (not isinstance(train_args.gradient_accumulation_steps, int)) or (
        train_args.gradient_accumulation_steps <= 0
    ):
        raise ValueError("gradient_accumulation_steps has to be an integer >= 1")

    # make sure to unset FSDP args when running on single gpu
    if not run_distributed:
        train_args.fsdp = ""
        train_args.fsdp_config = {"xla": False}

    task_type = "CAUSAL_LM"
    model = AutoModelForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        cache_dir=train_args.cache_dir,
        torch_dtype=get_torch_dtype(model_args.torch_dtype),
    )

    peft_config = get_hf_peft_config(task_type, peft_config)

    model.gradient_checkpointing_enable()

    # TODO: Move these to a config as well
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.model_name_or_path, cache_dir=train_args.cache_dir, use_fast=True
    )

    # TODO: understand if we need to hardcode these here or just use defaults in model
    if isinstance(tokenizer, LlamaTokenizer) or isinstance(
        tokenizer, LlamaTokenizerFast
    ):
        tokenizer.add_special_tokens(
            {
                "bos_token": "<s>",
                "eos_token": "</s>",
                "unk_token": "<unk>",
                "pad_token": "<pad>",
            }
        )
    elif isinstance(tokenizer, GPTNeoXTokenizerFast) or isinstance(
        tokenizer, GPT2Tokenizer
    ):
        tokenizer.add_special_tokens(
            {
                "pad_token": "<pad>",
            }
        )

    """TODO: near term - how response template ids are parsed out needs to be cleaned.
       The [2:] here applies if response template has \n prefix, it is needed to strip \n, otherwise template is not found.
       We will create issue to clean this out after we discuss data formats and collators we will support
    """
    response_template_ids = tokenizer.encode(
        data_args.response_template, add_special_tokens=False
    )[2:]
    # TODO: This is actually max_seq_length and not model_max_length. we should not override model_max_length
    # as in current main. We need to change name of this parameter we expose to users.
    model_max_length = min(train_args.model_max_length, tokenizer.model_max_length)
    logger.info(f"Model max length {model_max_length}")
    if train_args.model_max_length > tokenizer.model_max_length:
        logger.warning(
            f"model_max_length {train_args.model_max_length} exceeds tokenizer.model_max_length {tokenizer.model_max_length}, using tokenizer.model_max_length {tokenizer.model_max_length}"
        )

    # TODO: we need to change this, perhaps follow what open instruct does?
    special_tokens_dict = dict()
    if tokenizer.pad_token is None:
        logger.warning("PAD token set to default, missing in tokenizer")
        special_tokens_dict["pad_token"] = configs.DEFAULT_PAD_TOKEN
    if tokenizer.eos_token is None:
        logger.warning("EOS token set to default, missing in tokenizer")
        special_tokens_dict["eos_token"] = configs.DEFAULT_EOS_TOKEN
    if tokenizer.bos_token is None:
        logger.warning("BOS token set to default, missing in tokenizer")
        special_tokens_dict["bos_token"] = configs.DEFAULT_BOS_TOKEN
    if tokenizer.unk_token is None:
        logger.warning("UNK token set to default, missing in tokenizer")
        special_tokens_dict["unk_token"] = configs.DEFAULT_UNK_TOKEN

    # TODO: lower priority but understand if resizing impacts inference quality and why its needed.
    # It makes sense if we manipulate tokenizer that we also save it and provide it to inference.
    tokenizer_data_utils.tokenizer_and_embedding_resize(
        special_tokens_dict=special_tokens_dict,
        tokenizer=tokenizer,
        model=model,
    )

    # load the data by parsing JSON
    # TODO: update arg from data_path to training_data_path since we also have validation_data_path
    data_files = {"train": data_args.data_path}
    if data_args.validation_data_path:
        data_files["validation"] = data_args.validation_data_path

    format_dataset = lambda example: {
        f"{data_args.dataset_text_field}": example[f"{data_args.dataset_text_field}"]
        + tokenizer.eos_token
    }

    json_dataset = datasets.load_dataset("json", data_files=data_files)
    formatted_train_dataset = json_dataset["train"].map(format_dataset)
    logger.info(f"Training dataset length is {len(formatted_train_dataset)}")

    formatted_validation_dataset = None
    if data_args.validation_data_path:
        formatted_validation_dataset = json_dataset["validation"].map(format_dataset)
        logger.info(f"Validation dataset length is {len(formatted_validation_dataset)}")

    aim_callback = get_aimstack_callback()
    file_logger_callback = FileLoggingCallback(logger)
    peft_saving_callback = PeftSavingCallback()
    callbacks = [aim_callback, peft_saving_callback, file_logger_callback]

    if train_args.packing:
        logger.info("Packing is set to True")
        data_collator = None
        packing = True
    else:
        logger.info("Packing is set to False")
        if data_args.response_template is None:
            logger.error(
                "Error, response template is None, needs to be set for training"
            )
            exit(-1)

        if data_args.dataset_text_field is None:
            logger.error(
                "Error, dataset_text_field is None, needs to be set for training"
            )
            exit(-1)

        data_collator = DataCollatorForCompletionOnlyLM(
            response_template_ids,
            tokenizer=tokenizer,
            ignore_index=configs.IGNORE_INDEX,
        )
        packing = False

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=formatted_train_dataset,
        eval_dataset=formatted_validation_dataset,
        packing=packing,
        data_collator=data_collator,
        dataset_text_field=data_args.dataset_text_field,
        args=train_args,
        max_seq_length=model_max_length,
        callbacks=callbacks,
        peft_config=peft_config,
    )

    if run_distributed and peft_config is not None:
        trainer.accelerator.state.fsdp_plugin.auto_wrap_policy = fsdp_auto_wrap_policy(
            model
        )
    trainer.train()


def main(**kwargs):
    parser = transformers.HfArgumentParser(
        dataclass_types=(
            configs.ModelArguments,
            configs.DataArguments,
            configs.TrainingArguments,
            peft_config.LoraConfig,
            peft_config.PromptTuningConfig,
        )
    )
    parser.add_argument(
        "--peft_method",
        type=str.lower,
        choices=["pt", "lora", None, "none"],
        default="pt",
    )
    (
        model_args,
        data_args,
        training_args,
        lora_config,
        prompt_tuning_config,
        peft_method,
        _,
    ) = parser.parse_args_into_dataclasses(return_remaining_strings=True)
    if peft_method.peft_method == "lora":
        tune_config = lora_config
    elif peft_method.peft_method == "pt":
        tune_config = prompt_tuning_config
    else:
        tune_config = None
    train(model_args, data_args, training_args, tune_config)


if __name__ == "__main__":
    fire.Fire(main)
