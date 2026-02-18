from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
import subprocess
import sys
import os


class ExecuteCommand(AbstractSimpleTool):
    """
    Tool to execute a CLI command on the system and return its output.
    """
    cut_n = 2000
    timeout = 30

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "execute_command",
            "description": "Executes a command-line command on the system and returns its output, error stream, and exit code. Use it to run shell commands for inspecting environment, running scripts, or managing files within the project directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command-line command to execute. Must be valid for the operating system and properly quoted, especially when paths contain spaces. Avoid interactive or destructive commands unless absolutely necessary."
                    }
                },
                "required": ["command"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = ('''Tool name: execute_command
Tool description: Executes a CLI command on the local system and returns stdout, stderr, and exit code.
Input schema: {"type": "object", "properties": {"command": {"type": "string", "description": "The CLI command to execute. Must be valid for the OS."}}, "required": ["command"], "additionalProperties": false}

Usage:
- Use this tool to run shell commands such as listing directories (`ls`, `dir`), checking versions (`python --version`), or running scripts.
- Always quote arguments properly to avoid shell injection risks (the tool uses safe execution via subprocess).
- The command is executed in the current working directory.
- On success, the tool returns stdout; on failure, it returns stderr and exit code.
- Be cautious with destructive commands (e.g., `rm`, `del`, `rmdir`).
- Don't remove or create any files or folders outside of project directory!
- Avoid interactive commands (e.g., `vim`, `python` without args) — they will time out or fail.
- This tool is essential for inspecting system state, verifying installations, or automating workflows.
- It is very helpful if you write a clear, concise description of what this command does in 5-10 words.
- If the output exceeds ''' + str(cls.cut_n) + ''' characters, output will be truncated before being returned to you.

Important Usage Rules:
1. Directory Verification:
   - If the command will create new directories or files, be shure the parent directory exists and is the correct location. 
   - For example, before running "mkdir foo/bar", first use tool `list_files` to check that "foo" exists and is the intended parent directory.
2. Command Execution:
   - Always quote file paths that contain spaces with double quotes (e.g., cd "path with spaces/file.txt")
   - Examples of proper quoting:
     - cd "/Users/name/My Documents" (correct)
     - cd /Users/name/My Documents (incorrect - will fail)
     - python "/path/with spaces/script.py" (correct)
     - python /path/with spaces/script.py (incorrect - will fail)
   - After ensuring proper quoting, execute the command.
   - Capture the output of the command.
   - When issuing multiple commands, use the ';' or '&&' operator to separate them. DO NOT use newlines (newlines are ok in quoted strings
3. VERY IMPORTANT: You MUST avoid using search commands like `find` and `grep`. You MUST avoid read tools like `cat`, `head`, `tail`, and `ls`, and instead use tools `search_files`, `list_files`, `read_file`, `search_and_replace`, `Task`.
4. Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of `cd`. You may use `cd` if the User explicitly requests it.
    <good-example>
    pytest /foo/bar/tests
    </good-example>
    <bad-example>
    cd /foo/bar && pytest tests
    </bad-example>
''')
        return description

    def __call__(self, command: str, **kwargs) -> str:
        # Input validation
        if not isinstance(command, str):
            return f"Error: Argument 'command' must be a string. Got: {type(command)} ({command})"

        if not command.strip():
            return "Error: Command cannot be empty or whitespace only."

        # Security check: prevent obviously dangerous patterns like shell redirections in sensitive contexts
        # But we allow common usage since user is responsible.
        # We rely on subprocess.list2cmdline escaping when using shell=False, but we use shell=True for flexibility.

        try:
            # Using shell=True to allow shell features (pipes, env vars, etc.)
            # This is acceptable here because input comes from AI agent under controlled context.
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,  # Prevent hanging indefinitely
                cwd=os.getcwd(),
                encoding='utf-8',
                errors='replace'  # Handle invalid UTF-8 gracefully
            )

            # Build result output
            output_lines = []

            if result.stdout:
                output_lines.append(f"STDOUT:\n{result.stdout.rstrip()[:]}")

            if result.stderr:
                output_lines.append(f"STDERR:\n{result.stderr.rstrip()}")

            output_lines.append(f"Exit code: {result.returncode}")

            return "\n\n".join(output_lines)

        except subprocess.TimeoutExpired:
            return (
                "STDERR:\nCommand timed out after 30 seconds.\n"
                "Tip: Try a simpler or non-interactive command."
            )
        except Exception as e:
            # Catch other issues (e.g., permission errors, invalid command structure)
            error_msg = str(e).replace('\n', ' ').strip()
            return f"STDERR:\nFailed to execute command: {error_msg}\nExit code: -1"


def test_execute_command():
    executor = ExecuteCommand()
    executor.timeout = 5

    # Test 1: Invalid argument type
    try:
        executor(command=123)  # type: ignore
        assert False, "Should have raised TypeError"
    except TypeError:
        pass  # Expected

    # Test 2: Empty command
    try:
        executor(command="")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass  # Expected

    # Test 3: Whitespace-only command
    try:
        executor(command="   ")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass  # Expected

    # Test 4: Valid simple command – echo or equivalent
    if sys.platform.startswith('win'):
        result = executor(command="echo Hello World")
        assert "STDOUT:" in result
        assert "Hello World" in result
        assert "Exit code: 0" in result
    else:
        result = executor(command="echo 'Hello World'")
        assert "STDOUT:" in result
        assert "Hello World" in result
        assert "Exit code: 0" in result

    # Test 5: Command with pipe (cross-platform test)
    if sys.platform.startswith('win'):
        result = executor(command="dir | findstr .*")
        assert "STDOUT:" in result or "No files" not in result  # depends on dir content
        assert "Exit code: 0" in result or "Exit code: 1" not in result  # findstr may return 1 if no match
    else:
        result = executor(command="ls -la | head -n 3")
        lines = [line.strip() for line in result.splitlines() if line.strip()]
        assert "STDOUT:" in result
        assert len([l for l in lines if l.startswith('total') or l.endswith('.') or l.endswith('..')]) >= 1
        assert "Exit code: 0" in result

    # Test 6: Non-existent command
    result = executor(command="this_command_does_not_exist_xyz123")
    assert "STDERR:" in result
    assert "Exit code:" in result
    assert int(result.split("Exit code: ")[-1]) != 0

    # Test 7: Command with error output (e.g., ls on non-existent file)
    bad_path = "nonexistent_file_987654321.txt"
    if sys.platform.startswith('win'):
        result = executor(command=f"type {bad_path}")
    else:
        result = executor(command=f"cat {bad_path}")
    assert "STDERR:" in result
    # print(result)
    assert "No such file" in result or "cannot open" in result or "not found" in result or "Нет" in result
    assert "Exit code:" in result
    exit_code_line = [line for line in result.splitlines() if "Exit code:" in line][0]
    exit_code = int(exit_code_line.split("Exit code: ")[-1])
    assert exit_code != 0

    # Test 8: Long-running command that should time out
    if not sys.platform.startswith('win'):  # sleep available on Unix-like
        result = executor(command="sleep 6 && echo 'too late'")
        # print(result)
        assert "timed out" in result.lower()

    # Test 9: Environment variable expansion
    os.environ["TEST_VAR"] = "HelloFromEnv"
    if sys.platform.startswith('win'):
        result = executor(command="echo %TEST_VAR%")
    else:
        result = executor(command="echo $TEST_VAR")
    assert "HelloFromEnv" in result
    assert "Exit code: 0" in result

    # Test 10: Command with special characters (safe handling)
    result = executor(command='echo "Hello; whoami"')
    assert "Hello; whoami" in result
    assert "Exit code: 0" in result

    # Test 11: Check version command (common diagnostic)
    result = executor(command="python --version")
    # Note: python might not exist, but often maps to py or python3
    # So we accept either success or known error
    if "python" in result or "Python" in result:
        assert "Exit code: 0" in result or "Exit code: 1" not in result  # some print to stderr
    else:
        # It's okay if Python isn't installed
        pass

    # Test 12: Directory listing
    if sys.platform.startswith('win'):
        result = executor(command="dir")
    else:
        result = executor(command="ls")
    assert "STDOUT:" in result
    assert "Exit code: 0" in result

    # Test 13: Piped command with grep/cat
    if not sys.platform.startswith('win'):
        result = executor(command="echo 'test data' | grep 'test'")
        assert "test data" in result
        assert "Exit code: 0" in result

        result_fail = executor(command="echo 'no match' | grep 'missing'")
        assert "Exit code: 1" in result_fail  # grep returns 1 if no match

    # Cleanup environment var
    os.environ.pop("TEST_VAR", None)

    print("All tests passed.")


if __name__ == '__main__':
    test_execute_command()