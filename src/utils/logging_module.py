import logging
import os
import time
import config

logging.basicConfig(level=logging.DEBUG, handlers=[])
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.handlers.clear()  # Явно удаляем все обработчики

if config.config_dict.get('save_logs', False):
    logging_path = os.path.join(os.getcwd(), f'agent_{int(time.time())}.log')

    class FlushFileHandler(logging.FileHandler):
        def emit(self, record):
            super().emit(record)
            self.flush()

    handler = FlushFileHandler(logging_path, encoding='utf-8', mode='a')
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
else:
    logging_path = 'Null. Логи отключены. Включите логи в файле config.yml если они необходимы'
    logger.addHandler(logging.NullHandler())