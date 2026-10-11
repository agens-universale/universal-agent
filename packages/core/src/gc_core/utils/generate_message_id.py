import hashlib
from uuid import uuid4

from gc_core.execution.environment import supplied_id


def generate_msg_id_sha256(prefix):
    deterministic = supplied_id(prefix)
    if deterministic is not None:
        return deterministic
    guid = str(uuid4())
    hash_obj = hashlib.sha256(guid.encode("utf-8"))
    hex_digest = hash_obj.hexdigest()
    return f"{prefix}_{hex_digest[:32]}"


if __name__ == "__main__":
    print(generate_msg_id_sha256("fco"))
