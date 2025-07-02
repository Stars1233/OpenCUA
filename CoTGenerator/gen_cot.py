import json
import os
import traceback
import re
import backoff
import orjson
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError
from tqdm import tqdm
import concurrent.futures
from loguru import logger
from typing import List
from PIL import Image

from utils import (
    load_image, 
    image_to_base64,    
    draw_bounding_box_and_crop_patch,
    call_llm,
    )
from module.evaluator import (
    TRAJECTORY_EVAL_FORMAT_PROMPT, 
    FINAL_TRAJECTORY_EVAL_PROMPT,
    generate_traj_eval_history
    )

from module.generator import (
    COT_GENERATOR_PROMPT_FOR_MOUSE_ACTION,
    COT_GENERATOR_PROMPT_FOR_KEYBOARD_ACTION,
    REFLECT_COT_GENERATOR_PROMPT_FOR_MOUSE_ACTION,
    REFLECT_COT_GENERATOR_PROMPT_FOR_KEYBOARD_ACTION,
    parse_generator_response,
    DOUBLE_CHECK_PROMPT
)

from module.reflector_with_prior_judge import gen_reflection_thought_with_prior_judge
from module.reflector import gen_reflection_thought

def generate_all_history(generated_steps):
    previous_actions = [step['value']['action'] for step in generated_steps]

    if not previous_actions:
        return "None"
    
    history = ""
    for i in range(len(previous_actions)):
        history += f"Step {i+1}: {previous_actions[i]}\n"

    return history

@backoff.on_exception(
    backoff.expo,
    (Exception),
    max_time=180,  # Increase max_time to allow more retries
    max_tries=2,  # Limit the number of retries to prevent infinite loops
    jitter=backoff.full_jitter,  # Add jitter to spread out retry attempts
)
def generate_cot(
    client, 
    model:str,
    goal: str, 
    generated_steps: List[dict], 
    current_step_value: dict, 
    image: Image.Image, 
    image_patch: Image.Image = None, 
    next_image: Image.Image = None,
    need_double_check: bool = False,
    with_prior_judge: bool = False,
    skip_reflection: bool = False,
    ) -> dict:

    try:
        current_action = current_step_value['code']
        if not generated_steps:
            last_step_correct = True
            last_step_redundant = False
            former_thought = "None"
            former_action_effect = "None"
        else:
            last_step_correct = generated_steps[-1]['value']['last_step_correct']
            last_step_redundant = generated_steps[-1]['value'].get('last_step_redundant', False)
            former_thought = generated_steps[-1]['value']['thought']
            former_action_effect = generated_steps[-1]['value']['reflection']

        history_steps = generate_all_history(generated_steps)

        if last_step_correct and not last_step_redundant:
            if image_patch is None:
                prompt = COT_GENERATOR_PROMPT_FOR_KEYBOARD_ACTION
            else:
                prompt = COT_GENERATOR_PROMPT_FOR_MOUSE_ACTION
        else:
            if image_patch is None:
                prompt = REFLECT_COT_GENERATOR_PROMPT_FOR_KEYBOARD_ACTION
            else:
                prompt = REFLECT_COT_GENERATOR_PROMPT_FOR_MOUSE_ACTION

        prompt = prompt.format(
            goal=goal, 
            previous_actions=history_steps,
            former_thought=former_thought,
            former_action_effect=former_action_effect,
            action_commands=current_action
            )

        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_to_base64(image)}", "detail": "high"}}
        ]
        if image_patch is not None:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_to_base64(image_patch)}", "detail": "high"}})
        
        messages=[
                {
                    "role": "user",
                    "content": content,
                }
            ]
        response = call_llm(client, messages, model=model)

        print("First Response")

        if need_double_check:
            messages=[
                    {
                        "role": "user",
                        "content": content,
                    },
                    {
                        "role": "assistant",
                        "content": response, 
                    },
                    {
                        "role": "user",
                        "content": {"type": "text", "text": DOUBLE_CHECK_PROMPT}, 
                    }
                ]
            response = call_llm(client, messages, model=model)
            print("Double Check Response:")
            print(response)

        current_step = current_step_value.copy()
        current_step.update(parse_generator_response(response))
        current_step['code'] = current_action

        if not skip_reflection:
            if with_prior_judge:
                reflect_response = gen_reflection_thought_with_prior_judge(
                    client, 
                    goal=goal,
                    history_steps=history_steps,
                    current_step=current_step,
                    image=image,
                    image_patch=image_patch,
                    next_image=next_image,
                    )
            else:
                reflect_response = gen_reflection_thought(
                    client, 
                    goal=goal,
                    history_steps=history_steps,
                    current_step=current_step,
                    image=image,
                    image_patch=image_patch,
                    next_image=next_image,
                    )

            if with_prior_judge:
                current_step['last_step_redundant'] = not current_step['last_step_correct']
                current_step['reflection'] = reflect_response['reflection']
            else:
                current_step['last_step_correct'] = reflect_response['last_step_correct']
                current_step['last_step_redundant'] = reflect_response['last_step_redundant']
                current_step['reflection'] = reflect_response['reflection']
        else:
            current_step['last_step_correct'] = True
            current_step['last_step_redundant'] = False
            current_step['reflection'] = ""
            
        return current_step
    
    except (APITimeoutError, APIConnectionError, RateLimitError) as e:
        print("=" * 100)
        print(f"API Error Type: {type(e).__name__}")
        print(f"Error Message: {str(e)}")
        print(f"Retrying... (controlled by backoff decorator)")
        print("=" * 100)
        raise  

    except Exception as e:
        print("=" * 100)
        print(f"Unexpected Error Type: {type(e).__name__}")
        print(f"Error Message: {str(e)}")
        print("Traceback:")
        traceback.print_exc()
        print("=" * 100)
        raise


@backoff.on_exception(
    backoff.expo,
    (Exception),
    max_time=180,  # Increase max_time to allow more retries
    max_tries=2,  # Limit the number of retries to prevent infinite loops
    jitter=backoff.full_jitter,  # Add jitter to spread out retry attempts
)
def generate_traj_eval(generated_steps, goal, client, model):
    try:
        content = [
            {"type": "text", "text": TRAJECTORY_EVAL_FORMAT_PROMPT.format(goal=goal, steps=generate_traj_eval_history(generated_steps))+"\n\n"+FINAL_TRAJECTORY_EVAL_PROMPT,   }
        ]
        messages = [
            {
                "role": "user",
                "content": content,
            }
        ]
        response_str = call_llm(client, messages, model=model)
        print("Trajectory Evaluation:")
        print(response_str)
        if "```json" in response_str:
            response_str = response_str.split("```json")[1].split("```")[0].strip()
        parsed_data = orjson.loads(response_str)
        print("Parsed Eval Result:", parsed_data) 
        return parsed_data

    except (APITimeoutError, APIConnectionError, RateLimitError) as e:
        print("=" * 100)
        print(f"API Error Type: {type(e).__name__}")
        print(f"Error Message: {str(e)}")
        print(f"Retrying... (controlled by backoff decorator)")
        print("=" * 100)
        raise

    except Exception as e:
        logger.exception(f"Error generating trajectory evaluation: {str(e)}")
        raise


def process_traj(task, task_id, output_dir, image_folder, model:str, need_double_check=False, with_prior_judge=False):
    if "claude" in model.lower():
        base_url = "https://api.anthropic.com/v1/"
    elif "gpt" in model.lower():
        base_url = "https://api.openai.com/v1/"
    elif "gemini" in model.lower():
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    elif "qwen" in model.lower():
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    else:
        raise ValueError(f"Unsupported model: {model}. Please use a valid model name.")
    
    client=OpenAI(
        api_key=os.getenv('API_KEY'),
        base_url = base_url
    ) 

    goal = task['instruction']
    trajectory = task.pop('traj')
    
    meta = task.copy()
    output_dir = os.path.join(output_dir, task_id)
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "meta.json"), "w") as f:
        f.write(json.dumps(meta, ensure_ascii=False, indent=2))

    generated_steps = []
    
    for i, step in tqdm(enumerate(trajectory)):
        try:
            if os.path.exists(os.path.join(output_dir, f"{i:03}.json")):
                # logger.info(f"Step {i} already processed, skipping.")
                with open(os.path.join(output_dir, f"{i:03}.json"), encoding='utf-8') as f:
                    step = json.load(f)
                assert step['index'] == i, f"Step index mismatch: {step['index']} != {i}"
                generated_steps.append(step)
                continue

            image_with_bbox = load_image(step['image'], image_folder)        
            image_with_bbox, image_patch = draw_bounding_box_and_crop_patch(image_with_bbox, step['value']['code'])
            
            next_image = None
            if i <len(trajectory)-1:
                next_image = load_image(trajectory[i+1]['image'], image_folder)

            code = step['value']['code']
            if not code:
                logger.info(f"Step {i} code is empty, skipping.")
                continue
            
            response = generate_cot(
                client = client, 
                model = model,
                goal = goal, 
                generated_steps=generated_steps, 
                current_step_value=step['value'], 
                image = image_with_bbox, 
                image_patch=image_patch,
                next_image=next_image,
                need_double_check=need_double_check,
                with_prior_judge=with_prior_judge,
                skip_reflection= True if i == len(trajectory)-1 else False,
                )
        
            result = {
                'index':i, 
                'image': step['image'], 
                'value': {**response, 'code': code}
                }
            generated_steps.append(result)

            with open(os.path.join(output_dir, f"{i:03}.json"), "w") as f:
                f.write(json.dumps(result, ensure_ascii=False, indent=2))

            logger.info(f"Success: Processed task {task_id} step {i}")

            if len(generated_steps) == len(trajectory): 
                logger.info("Generating trajectory evaluation...")
                eval_result = generate_traj_eval(generated_steps, goal, client, model)
                with open(os.path.join(output_dir, "meta.json"), "r") as f:
                    meta = json.load(f)
                meta.update(eval_result)
                with open(os.path.join(output_dir, "meta.json"), "w") as f:
                    f.write(json.dumps(meta, ensure_ascii=False, indent=2))

        except Exception as e:
            logger.exception(f"Error processing {task_id} step {i}: {str(e)}")
            break
    
    logger.info(f"Done: Processed task {task_id}")


def gen_inner_monologue_mt(image_folder, traj_path, output_dir, model="claude-3-7-sonnet-20250219", num_threads = 10, max_num = None, need_double_check=False, with_prior_judge=False):
    tasks = []
    with open(traj_path, 'r') as file:
        for line in file:
            tasks.append(json.loads(line.strip()))

    existing_files = os.listdir(output_dir)
    required_tasks = []
    done_tasks_count = 0
    continue_task_count = 0
    for task in tqdm(tasks):
        if "task_id" not in task:
            task['task_id'] = task['traj'][0]['image'].rsplit(".",1)[0].split("/")[-1].rsplit("_", 1)[0]
            task_id = task['task_id']
        else:
            task_id = task['task_id']

        if task_id not in existing_files:
            required_tasks.append(task)
            continue
        
        if len(os.listdir(os.path.join(output_dir, task_id))) < len(task['traj']) + 1:
            required_tasks.append(task)
            continue_task_count += 1
        else:
            done_tasks_count += 1

    if max_num is not None:
        tasks = required_tasks[:max_num]
    else:
        tasks = required_tasks

    logger.info(f"Total tasks: {len(tasks)}")
    logger.info(f"Tasks already done: {done_tasks_count}")
    logger.info(f"Tasks to continue: {continue_task_count}")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = []
        for task in tasks:  
            task_id = task['task_id']        
            futures.append(executor.submit(process_traj, task, task_id, output_dir, image_folder, model, need_double_check, with_prior_judge))

        for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures)):
            _ = future.result()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate Inner Monologue")
    parser.add_argument("--image_folder", type=str, default="./OpenCUA-Code/CoTGenerator/example/images", help="Path to the image folder")
    parser.add_argument("--traj_path", type=str, default='./OpenCUA-Code/CoTGenerator/example/traj_example.jsonl', help="Path to the trajectory file")
    parser.add_argument("--output_dir", type=str, default="./OpenCUA-Code/CoTGenerator/example/output", help="Output directory for generated files")
    
    parser.add_argument("--need_double_check", action='store_true', help="Whether to perform double check on the generated code")
    parser.add_argument("--with_prior_judge", action='store_true', help="Whether there is a judge result in the step. True for Ubuntu; False for AGN")
    
    parser.add_argument("--model", type=str, default="claude-3-7-sonnet-20250219", help="Model to use for LLM calls")
    parser.add_argument("--num_threads", type=int, default=1, help="Number of threads to use")
    parser.add_argument("--max_num", type=int, default=None, help="Maximum number of tasks to process")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    args = dict(args._get_kwargs())
    gen_inner_monologue_mt(**args)


if __name__ == "__main__":
    main()
