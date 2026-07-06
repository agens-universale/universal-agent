import json
import hashlib
from copy import deepcopy
from typing import Callable, Dict, List, Optional

from utils.backend_errors import normalize_finish_reason

_MAX_BACKEND_LOG_CHARS = 8000


def _safe_json_for_log(payload, *, max_chars=_MAX_BACKEND_LOG_CHARS):
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        raw = str(payload)
    if len(raw) <= max_chars:
        return raw
    return f"{raw[:max_chars]}... [truncated, total_chars={len(raw)}]"


def sanitize_messages_for_chat_completions(
    messages: List[Dict],
    *,
    invalid_tool_call_factory: Optional[Callable[[Dict], Dict]] = None,
) -> List[Dict]:
    """
    Техническая нормализация истории перед chat/completions:
    function_call с невалидным JSON в arguments заменяется фабрикой,
    либо пропускается, если фабрика не передана.
    """
    sanitized: List[Dict] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("type") != "function_call":
            sanitized.append(message)
            continue

        raw_args = message.get("arguments")
        is_valid_json_str = isinstance(raw_args, str) and bool(raw_args.strip())
        if is_valid_json_str:
            try:
                json.loads(raw_args)
                sanitized.append(message)
                continue
            except Exception:  # noqa: BLE001
                pass

        if callable(invalid_tool_call_factory):
            replaced = invalid_tool_call_factory(message)
            if isinstance(replaced, dict):
                sanitized.append(replaced)
        # Без фабрики просто не отправляем невалидный function_call.
    return sanitized


def get_md5_hash(obj):
    """Returns an md5 hash of a Python object for consistent IDs."""
    # For dicts/lists, use json serialization for stable hashing
    s = json.dumps(obj)
    return hashlib.md5(s.encode('utf-8')).hexdigest()


def get_output_list(dct, logger_override=None):
    """Transforms chat completion 'choices' into a standardized response 'output' list."""
    output_list = []
    for dct_choice in dct['choices']:
        choice_error = dct_choice.get("error")
        if isinstance(choice_error, dict):
            err_code = choice_error.get("code")
            err_message = choice_error.get("message")
            err_metadata = choice_error.get("metadata")
            raise ValueError(
                "Chat completion provider returned choice error: "
                f"code={err_code!r} message={err_message!r} metadata={err_metadata!r}"
            )
        fr = dct_choice.get("finish_reason")
        msg_obj = dct_choice.get("message") or {}
        _decision = normalize_finish_reason(
            fr,
            has_content=bool(msg_obj.get("content")),
            has_tool_calls=bool(msg_obj.get("tool_calls")),
        )
        if not _decision.accept:
            if logger_override is not None:
                logger_override.error(
                    "Empty/unusable chat/completions response (finish_reason=%r, "
                    "no content/tool_calls): payload=%s",
                    fr, _safe_json_for_log(dct),
                )
            raise ValueError(
                f"Backend returned empty/unusable response (finish_reason={fr!r})"
            )
        if _decision.unknown and logger_override is not None:
            logger_override.warning(
                "Unknown finish_reason=%r accepted permissively (content/tool_calls "
                "present): payload=%s",
                fr, _safe_json_for_log(dct),
            )
        if fr == "length" and logger_override is not None:
            logger_override.warning(
                "chat/completions choice finished with length (output may be truncated; "
                "tool JSON may be incomplete). payload=%s",
                _safe_json_for_log(dct),
            )

        # --- FIX 1: Check for None content before creating a message object ---
        if dct_choice['message'].get('content') is not None:
            id_choice = get_md5_hash(dct_choice)
            output_list.append({
                "id": "msg_" + id_choice,
                "type": "message",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "annotations": [],
                    "logprobs": [],
                    "text": dct_choice['message']['content']
                }],
                "role": dct_choice['message']['role']
            })

        # --- FIX 2: Generate a unique ID for each tool call ---

        if dct_choice['message'].get("tool_calls", None) is not None:
            for tool_call_dict in dct_choice['message']['tool_calls']:
                if tool_call_dict['type'] != 'function':
                    raise ValueError("Unknown logic")

                # Generate a unique hash for THIS specific tool call
                id_tool_call = get_md5_hash(tool_call_dict)

                output_list.append({
                    "arguments": tool_call_dict['function']['arguments'],
                    "call_id": tool_call_dict['id'],
                    "name": tool_call_dict['function']['name'],
                    "type": "function_call",
                    "id": "fc_" + id_tool_call,  # Use the new unique ID
                    "status": "completed"
                })

    return output_list


def output_chat_completion_to_response(dct, logger_override=None, log_context=None):
    """Converts a chat completion response dict to a model response format."""
    dct_output = deepcopy(dct)
    dct_output['id'] = dct_output.get('id', get_md5_hash(dct_output))
    # Backends may return either `created` (chat/completions-like)
    # or already normalized `created_at`.
    created = dct_output.pop("created", None)
    if created is not None:
        dct_output["created_at"] = created
    elif "created_at" not in dct_output:
        dct_output["created_at"] = None

    try:
        dct_output['output'] = get_output_list(
            dct_output, logger_override=logger_override
        )
    except Exception:
        if logger_override is not None:
            logger_override.error(
                "Failed to convert chat/completions payload. context=%s payload=%s",
                _safe_json_for_log(log_context or {}),
                _safe_json_for_log(dct),
                exc_info=True,
            )
        raise
    if 'system_fingerprint' in dct_output:
        del dct_output['system_fingerprint']

    dct_output['parallel_tool_calls'] = True
    dct_output['status'] = "completed"
    dct_output['object'] = 'response'
    return dct_output


def tool_responces_to_chat_completion(tool):
    """tool format v1/responses -> v1/chat/completion"""
    return {"type": tool['type'],
            "function": {"name": tool['name'],
                         "description": tool['description'],
                         "parameters": {"type": tool['parameters']['type'],
                                        "properties": tool['parameters']['properties'],
                                        "required": tool['parameters']['required'],
                                        }
                         }

            }


def tool_chat_completion_to_responces(tool):
    """tool format v1/chat/completion -> v1/responses"""
    tool_res = {"type": tool['type'],
                "name": tool['function']['name'],
                "description": tool['function']["description"],
                "parameters": tool['function']['parameters'],
                "strict": True
                }
    # --- FIX 3: Corrected typo 'parametres' to 'parameters' ---
    tool_res['parameters']['additionalProperties'] = tool_res['parameters'].get(
        'additionalProperties', False)
    return tool_res


if __name__ == '__main__':
    example = {'id': 'chatcmpl-1c538156eca8494ca45d25829c897c49', 'object': 'response', 'model': 'CodeChat', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': "name: javascript_browser_game_developer  \ndescription: Expert in developing engaging 2D browser-based games using JavaScript and modern game development frameworks such as Phaser, PixiJS, or plain Canvas API. Skilled in game loop management, sprite animation, input handling, collision detection, audio integration, and performance optimization for web environments.\n\nYou are a professional browser game developer responsible for building interactive, responsive, and visually appealing 2D games that run smoothly across devices and browsers. When assigned a game development task:\n\n- Use modern, readable JavaScript (ES6+ syntax: classes, modules, async/await, destructuring).\n- Prefer framework-agnostic patterns when possible; default to **Phaser 3** unless specified otherwise.\n- Structure code cleanly: separate game scenes, entities, utilities, and configuration.\n- Implement smooth animations and responsive controls (keyboard, mouse, touch).\n- Optimize for 60 FPS performance; minimize garbage collection and layout thrashing.\n- Ensure cross-browser compatibility (Chrome, Firefox, Safari, Edge) and mobile responsiveness.\n- Integrate audio with fallbacks and user interaction guards (due to autoplay policies).\n\nGame Development Process:  \n1. **Clarify scope**: Identify game genre (platformer, puzzle, arcade, etc.), core mechanics, win/lose conditions, and assets needed (sprites, sounds, fonts).  \n2. **Design architecture**:  \n   - Break game into logical scenes (Boot, Preload, Menu, Game, GameOver).  \n   - Define player, enemies, items, UI, and physics ( Arcade or Matter.js ).  \n   - Plan state management (score, lives, levels, inventory).  \n3. **Implement core loop**:  \n   - Set up the game config (width, height, physics, renderer).  \n   - Load assets efficiently (preload with progress bar if needed).  \n   - Code update-render cycle with delta-time handling for smooth motion.  \n4. **Develop mechanics**:  \n   - Implement player movement, collision, jumping, shooting, or other actions.  \n   - Add enemy AI, power-ups, scoring, and level progression.  \n   - Handle input with clear feedback (e.g., key highlights, touch buttons).  \n5. **Enhance experience**:  \n   - Add particle effects, transitions, screen shake, and visual polish.  \n   - Integrate sound effects and background music with mute controls.  \n   - Design clean UI (score display, health bar, pause menu).  \n6. **Test and optimize**:  \n   - Debug physics and timing issues.  \n   - Profile performance on low-end devices.  \n   - Ensure accessibility (color contrast, keyboard nav, resize support).  \n\nCode Standards:  \n- Write modular, reusable components (e.g., `Player.js`, `EnemySpawner.js`, `UIManager.js`).  \n- Use JSDoc comments for complex functions or class interfaces.  \n- Follow consistent naming: `camelCase` for variables, `PascalCase` for classes.  \n- Avoid global variables; encapsulate game state within scenes or state managers.  \n- Prefer composition over deep inheritance where possible.  \n- Use `const`/`let`; avoid `var`.  \n- Format code cleanly (align with Prettier defaults).  \n\nAsset Handling:  \n- Assume assets are provided in standard formats: PNG (sprites), MP3/Ogg (audio), JSON (tilemaps, configs).  \n- Use relative paths: `assets/images/player.png`, `assets/audio/jump.mp3`.  \n- Support dynamic loading and caching.  \n- Include fallbacks for missing or failed-to-load assets.  \n\nFramework Guidance (Phaser 3 default):  \n- Use `this.load.image()`, `this.load.spritesheet()`, `this.load.audio()` in Preload scene.  \n- Leverage built-in physics: `this.physics.add.*` for collisions and movement.  \n- Use `Scene` system for flow control (`this.scene.start('GameOver')`).  \n- Emit and listen to events for decoupled communication (e.g., `this.events.emit('score', 100)`).  \n\nOutput Format:  \nReturn only the essential deliverables:  \n1. Main game code (JavaScript modules or scene files with full implementation).  \n2. HTML host file (minimal boilerplate with canvas container and script loading).  \n3. CSS (if needed for fullscreen, centering, or UI styling).  \n4. Configuration files (e.g., `game.config.js`, `webpack.config.js` if requested).  \n5. Asset structure guide (folder layout and naming convention).  \n\nIf multiple files are generated, list them at the end with brief comments:  \n> **Artifacts created:**  \n> - `game.js`: Main Phaser game instance and scene registration  \n> - `scenes/PlayScene.js`: Core gameplay logic with player, enemies, and scoring  \n> - `entities/Player.js`: Player character with movement and input handling  \n> - `index.html`: Entry point with canvas and mobile viewport settings  \n> - `style.css`: Responsive styling for full-window game display  \n\nGuidelines:  \n- Never return incomplete code or stubs (e.g., empty functions, `// TODO`).  \n- Do not invent external APIs unless requested (e.g., leaderboards, auth).  \n- If design decisions arise (e.g., framework choice, physics type), explain briefly.  \n- Assume touch support is required for mobile compatibility.  \n- Respect browser autoplay policies: initialize audio on first user interaction.  \n\nYour game will be run directly in a browser — it must work out of the box with no errors.  \nReturn final result via `finish` tool call with all artifacts included.", 'reasoning_content': '', 'tool_calls': []}, 'logprobs': None, 'finish_reason': 'stop', 'matched_stop': None}], 'usage': {'prompt_tokens': 1425, 'total_tokens': 2584, 'completion_tokens': 1159, 'prompt_tokens_details': None}, 'created_at': 1763459496, 'output': [{'id': 'msg_a3c4717784ada8856853122210d8e295', 'type': 'message', 'status': 'completed', 'content': [{'type': 'output_text', 'annotations': [], 'logprobs': [], 'text': "name: javascript_browser_game_developer  \ndescription: Expert in developing engaging 2D browser-based games using JavaScript and modern game development frameworks such as Phaser, PixiJS, or plain Canvas API. Skilled in game loop management, sprite animation, input handling, collision detection, audio integration, and performance optimization for web environments.\n\nYou are a professional browser game developer responsible for building interactive, responsive, and visually appealing 2D games that run smoothly across devices and browsers. When assigned a game development task:\n\n- Use modern, readable JavaScript (ES6+ syntax: classes, modules, async/await, destructuring).\n- Prefer framework-agnostic patterns when possible; default to **Phaser 3** unless specified otherwise.\n- Structure code cleanly: separate game scenes, entities, utilities, and configuration.\n- Implement smooth animations and responsive controls (keyboard, mouse, touch).\n- Optimize for 60 FPS performance; minimize garbage collection and layout thrashing.\n- Ensure cross-browser compatibility (Chrome, Firefox, Safari, Edge) and mobile responsiveness.\n- Integrate audio with fallbacks and user interaction guards (due to autoplay policies).\n\nGame Development Process:  \n1. **Clarify scope**: Identify game genre (platformer, puzzle, arcade, etc.), core mechanics, win/lose conditions, and assets needed (sprites, sounds, fonts).  \n2. **Design architecture**:  \n   - Break game into logical scenes (Boot, Preload, Menu, Game, GameOver).  \n   - Define player, enemies, items, UI, and physics ( Arcade or Matter.js ).  \n   - Plan state management (score, lives, levels, inventory).  \n3. **Implement core loop**:  \n   - Set up the game config (width, height, physics, renderer).  \n   - Load assets efficiently (preload with progress bar if needed).  \n   - Code update-render cycle with delta-time handling for smooth motion.  \n4. **Develop mechanics**:  \n   - Implement player movement, collision, jumping, shooting, or other actions.  \n   - Add enemy AI, power-ups, scoring, and level progression.  \n   - Handle input with clear feedback (e.g., key highlights, touch buttons).  \n5. **Enhance experience**:  \n   - Add particle effects, transitions, screen shake, and visual polish.  \n   - Integrate sound effects and background music with mute controls.  \n   - Design clean UI (score display, health bar, pause menu).  \n6. **Test and optimize**:  \n   - Debug physics and timing issues.  \n   - Profile performance on low-end devices.  \n   - Ensure accessibility (color contrast, keyboard nav, resize support).  \n\nCode Standards:  \n- Write modular, reusable components (e.g., `Player.js`, `EnemySpawner.js`, `UIManager.js`).  \n- Use JSDoc comments for complex functions or class interfaces.  \n- Follow consistent naming: `camelCase` for variables, `PascalCase` for classes.  \n- Avoid global variables; encapsulate game state within scenes or state managers.  \n- Prefer composition over deep inheritance where possible.  \n- Use `const`/`let`; avoid `var`.  \n- Format code cleanly (align with Prettier defaults).  \n\nAsset Handling:  \n- Assume assets are provided in standard formats: PNG (sprites), MP3/Ogg (audio), JSON (tilemaps, configs).  \n- Use relative paths: `assets/images/player.png`, `assets/audio/jump.mp3`.  \n- Support dynamic loading and caching.  \n- Include fallbacks for missing or failed-to-load assets.  \n\nFramework Guidance (Phaser 3 default):  \n- Use `this.load.image()`, `this.load.spritesheet()`, `this.load.audio()` in Preload scene.  \n- Leverage built-in physics: `this.physics.add.*` for collisions and movement.  \n- Use `Scene` system for flow control (`this.scene.start('GameOver')`).  \n- Emit and listen to events for decoupled communication (e.g., `this.events.emit('score', 100)`).  \n\nOutput Format:  \nReturn only the essential deliverables:  \n1. Main game code (JavaScript modules or scene files with full implementation).  \n2. HTML host file (minimal boilerplate with canvas container and script loading).  \n3. CSS (if needed for fullscreen, centering, or UI styling).  \n4. Configuration files (e.g., `game.config.js`, `webpack.config.js` if requested).  \n5. Asset structure guide (folder layout and naming convention).  \n\nIf multiple files are generated, list them at the end with brief comments:  \n> **Artifacts created:**  \n> - `game.js`: Main Phaser game instance and scene registration  \n> - `scenes/PlayScene.js`: Core gameplay logic with player, enemies, and scoring  \n> - `entities/Player.js`: Player character with movement and input handling  \n> - `index.html`: Entry point with canvas and mobile viewport settings  \n> - `style.css`: Responsive styling for full-window game display  \n\nGuidelines:  \n- Never return incomplete code or stubs (e.g., empty functions, `// TODO`).  \n- Do not invent external APIs unless requested (e.g., leaderboards, auth).  \n- If design decisions arise (e.g., framework choice, physics type), explain briefly.  \n- Assume touch support is required for mobile compatibility.  \n- Respect browser autoplay policies: initialize audio on first user interaction.  \n\nYour game will be run directly in a browser — it must work out of the box with no errors.  \nReturn final result via `finish` tool call with all artifacts included."}], 'role': 'assistant'}], 'parallel_tool_calls': True, 'status': 'completed'}
    print(output_chat_completion_to_response(example))
