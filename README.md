# FMS HF Tuning

This repo provides basic tuning scripts with support for specific models. The repo relies on Hugging Face `SFTTrainer` and PyTorch FSDP. Our approach to tuning is:
1. Models are loaded from Hugging Face `transformers` or the [foundation-model-stack](https://github.com/foundation-model-stack/foundation-model-stack) -- models are either optimized to use `Flash Attention v2` directly or through `SDPA`
2. Hugging Face `SFTTrainer` for the training loop
3. `FSDP` as the backend for training

## Installation

```
pip install -r requirements.txt
pip install -U datasets
pip install -e .
```

> Note: If you wish to use [FlashAttention](https://github.com/Dao-AILab/flash-attention), then you need to install these requirements: `pip install -r flashattn_requirements`. [FlashAttention](https://github.com/Dao-AILab/flash-attention) requires the [CUDA Toolit](https://developer.nvidia.com/cuda-toolkit) to be pre-installed.

## Data format
The data format expectation is a single column text. The trainer is configured to expect a response template as a string. For example, if one wants to prepare the `alpaca` format data to feed into this trainer, it is quite easy and can be done with the following code.

```python
PROMPT_DICT = {
    "prompt_input": (
        "Below is an instruction that describes a task, paired with an input that provides further context. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Input:\n{input}\n\n### Response:"
    ),
    "prompt_no_input": (
        "Below is an instruction that describes a task. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Response:"
    ),
}

def format_alpaca_fn(example):
    prompt_input, prompt_no_input = PROMPT_DICT['prompt_input'], PROMPT_DICT['prompt_no_input']
    output = prompt_input.format_map(example) if example.get("input", "") != "" else prompt_no_input.format_map(example)
    output = f"{output} {example['output']}"
    return {"output": output}

ds = datasets.load_dataset('json', data_files='./stanford_alpaca/alpaca_data.json')

alpaca_ds = ds['train'].map(format_alpaca_fn, remove_columns=['instruction', 'input'])
alpaca_ds.to_json("sft_alpaca_data.json")
```

The `response template` corresponding to the above dataset and the `Llama` tokenizer is: `\n### Response:"`.

The same way can be applied to any dataset, with more info can be found [here](https://huggingface.co/docs/trl/main/en/sft_trainer#format-your-input-prompts).


## Supported Models

Current supported and tested models are `Llama2` (7 and 13B configurations have been tested) and `GPTBigCode`.

## Training

### Single GPU
```bash
# if you want to use one GPU on multi-gpu machine
export CUDA_VISIBLE_DEVICES=0

python tuning/sft_trainer.py  \
--model_name_or_path $MODEL_PATH  \
--data_path $DATA_PATH  \
--output_dir $OUTPUT_PATH  \
--num_train_epochs 5  \
--per_device_train_batch_size 4  \
--per_device_eval_batch_size 4  \
--gradient_accumulation_steps 4  \
--evaluation_strategy "no"  \
--save_strategy "epoch"  \
--learning_rate 1e-5  \
--weight_decay 0.  \
--warmup_ratio 0.03  \
--lr_scheduler_type "cosine"  \
--logging_steps 1  \
--include_tokens_per_second  \
--packing False  \
--response_template "\n### Response:"  \
--dataset_text_field "output" 

```

### Multiple GPUs with FSDP
```bash
torchrun \
--nnodes=1 \
--nproc_per_node=8 \ 
--master_port=1234 \
tuning/sft_trainer.py \
--model_name_or_path $MODEL_PATH \
--data_path $DATA_PATH \
--bf16 True \
--output_dir $OUTPUT_PATH \
--num_train_epochs 5 \
--per_device_train_batch_size 4 \
--per_device_eval_batch_size 4 \
--gradient_accumulation_steps 4 \
--evaluation_strategy "no" \
--save_strategy "epoch" \
--learning_rate 1e-5 \
--weight_decay 0. \
--warmup_ratio 0.03 \
--lr_scheduler_type "cosine" \
--logging_steps 1 \
--fsdp "full_shard auto_wrap" \ 
--fsdp_config tuning/config/fsdp_config.json \
--include_tokens_per_second \
--packing False \
--response_template "\n### Response:" \
--dataset_text_field "output"
```


For `GPTBigCode` models, Hugging Face has enabled Flash v2 and one can simply replace the `'LlamaDecoderLayer'` with `'GPTBigCodeBlock'` in `tuning/config/fsdp_config.json` for proper sharding of the model.

## Inference
Currently, we do *not* offer inference support as part of the library, but we provide a standalone script for running inference on tuned models for testing purposes. For a full list of options run `python scripts/run_inference.py --help`. Note that no data formatting / templating is applied at inference time.

### Running a single example
If you want to run a single example through a model, you can pass it with the `--text` flag.

```bash
python scripts/run_inference.py \
--model my_checkpoint \
--text "This is a text the model will run inference on" \
--max_new_tokens 50 \
--out_file result.json
```

### Running multiple examples
To run multiple examples, pass a path to a file containing each source text as its own line. Example:

Contents of `source_texts.txt`
```
This is the first text to be processed.
And this is the second text to be processed.
```

```bash
python scripts/run_inference.py \
--model my_checkpoint \
--text_file source_texts.txt \
--max_new_tokens 50 \
--out_file result.json
```

### Inference Results Format
After running the inference script, the specified `--out_file` will be a JSON file, where each text has the original input string and the predicted output string, as follows. Note that due to the implementation of `.generate()` in Transformers, in general, the input string will be contained in the output string as well.
```
[
    {
        "input": "{{Your input string goes here}}",
        "output": "{{Generate result of processing your input string goes here}}"
    },
    ...
]
```

### Changing the Base Model for Inference
If you tuned a model using a *local* base model, then a machine-specific path will be saved into your checkpoint by Peft, specifically the `adapter_config.json`. This can be problematic if you are running inference on a different machine than you used for tuning.

As a workaround, the CLI for inference provides an arg for `--base_model_name_or_path`, where a new base model may be passed to run inference with. This will patch the `base_model_name_or_path` in your checkpoint's `adapter_config.json` while loading the model, and restore it to its original value after completion. Alternatively, if you like, you can change the config's value yourself.

NOTE: This can also be an issue for tokenizers (with the `tokenizer_name_or_path` config entry). We currently do not allow tokenizer patching since the tokenizer can also be explicitly configured within the base model and checkpoint model, but may choose to expose an override for the `tokenizer_name_or_path` in the future.

## Validation

We can use [`lm-evaluation-harness`](https://github.com/EleutherAI/lm-evaluation-harness) from EleutherAI for evaluating the generated model. For example, for the Llama-13B model, using the above command and the model at the end of Epoch 5, we evaluated MMLU score to be `53.9` compared to base model to be `52.8`.

How to run the validation:
```bash
pip install -U transformers
pip install -U datasets
git clone https://github.com/EleutherAI/lm-evaluation-harness
cd lm-evaluation-harness
pip install -e .
python main.py \ 
--model hf-causal \
--model_args pretrained=$MODEL_PATH \ 
--output_path $OUTPUT_PATH/results.json \ 
--tasks boolq,piqa,hellaswag,winogrande,arc_easy,arc_challenge,hendrycksTest-*
```

The above runs several tasks with `hendrycksTest-*` being MMLU.

## More Examples

[Prompt Tuning on Twitter Complaints](examples/prompt_tuning_twitter_complaints/README.md)

