import os

from tools.simple_tools.abstract_simple_tool import  AbstractSimpleTool

class WriteToFile(AbstractSimpleTool):

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "write_to_file",
            "description": "Writes content to a specified file on the local filesystem, overwriting it if it already exists. This tool must only be used after reading the file if it exists, and should primarily be used for modifying existing files rather than creating new ones unless explicitly instructed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the file to write to. Must be an absolute path, not relative. Ensure the file location is valid and accessible."
                    },
                    "content": {
                        "type": "string",
                        "description": "The full text content to write into the file. This will completely replace the existing file content. Always include complete and correct syntax when writing code or structured data."
                    }
                },
              "required": ["path", "content"],
              "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: write_to_file
Tool description: Writes a file to the local filesystem.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The absolute path of the file to write to (must be absolute, not relative)."}, "content": {"type": "string", "description": "The complete content to write to the file."}}, "required": ["path", "content"], "additionalProperties": false}

Usage:
- This tool will overwrite the existing file if there is one at the provided path.
- If this is an existing file, you MUST use the Read tool first to read the file's contents. This tool will fail if you did not read the file first.
- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
- Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked.
'''
        return description

    def __call__(self, path: str, content: str, **kwargs) -> str:
        if not isinstance(path, str):
            return  f'Error: variable path should be string. Got: {path}'

        if not isinstance(content, str):
            return f'Error: variable path should be string. Got: {content}'

        try:
            with open(path, 'w', encoding='utf8') as fd:
                fd.write(content)
            message = f'content written to file "{path}"'
        except Exception as e:
            message = str(e)

        return message


def test_write_to_file():
    content = "lorem_ipsum"
    path = './test_file.txt'
    wt = WriteToFile()
    wt(path=path, content=content)
    with open(path, 'r') as fd:
        data = fd.read()

    assert data == content
    os.remove(path)

if __name__ == '__main__':
    test_write_to_file()
