import os

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool

class AppendToFile(AbstractSimpleTool):

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "append_to_file",
            "description": "Appends content to the end of an existing file. Useful for adding notes, logs, or analysis results. The file must already exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the existing file to append to."
                    },
                    "content": {
                        "type": "string",
                        "description": "The text content to append to the file."
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
        description = '''Tool name: append_to_file
Tool description: Appends content to an existing file (e.g., for notes, logs, analysis results).
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute path to an existing file."}, "content": {"type": "string", "description": "Content to append."}}, "required": ["path", "content"], "additionalProperties": false}

Usage:
- The target file MUST exist. If the file does not exist, use create_file or write_to_file first.
- The tool automatically adds a newline separator before the new content to ensure distinct entries.
- Ideal for accumulating analysis notes, debugging logs, or review comments without overwriting previous data.
- Do not use for modifying source code logic.
'''
        return description

    def __call__(self, path: str, content: str, **kwargs) -> str:
        if not isinstance(path, str):
            return f'Error: variable path should be string. Got: {path}'

        if not isinstance(content, str):
            # Исправлена опечатка из оригинального класса (было 'variable path' для content)
            return f'Error: variable content should be string. Got: {content}'

        # Проверка существования файла
        if not os.path.exists(path):
            return f'Error: File not found at path "{path}". Cannot append to a non-existent file.'

        try:
            # Открываем в режиме добавления ('a')
            with open(path, 'a', encoding='utf8') as fd:
                # Добавляем пропуск в одну строку (\n\n) перед новым контентом
                # Это гарантирует визуальное разделение блоков текста
                fd.write('\n\n' + content)
            
            message = f'content appended to file "{path}"'
        except Exception as e:
            message = str(e)

        return message


def test_append_to_file():
    path = './test_append_review.txt'
    
    # 1. Сначала создаем файл (так как append требует существования)
    with open(path, 'w', encoding='utf8') as fd:
        fd.write("Initial analysis note.")
    
    # 2. Тестируем добавление
    at = AppendToFile()
    result = at(path=path, content="Second analysis note.")
    
    assert "content appended" in result, f"Expected success, got: {result}"
    
    with open(path, 'r', encoding='utf8') as fd:
        data = fd.read()
    
    # Ожидаем: исходный текст + 2 переноса строки + новый текст
    expected = "Initial analysis note.\n\nSecond analysis note."
    assert data == expected, f"Expected:\n{repr(expected)}\nGot:\n{repr(data)}"
    
    # 3. Тестируем ошибку при отсутствии файла
    fake_path = './non_existent_review.txt'
    result_error = at(path=fake_path, content="test")
    assert "Error: File not found" in result_error, f"Expected error, got: {result_error}"
    
    # Очистка
    os.remove(path)
    print("All tests passed.")

if __name__ == '__main__':
    test_append_to_file()