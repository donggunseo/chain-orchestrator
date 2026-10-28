"""Read only explicitly requested synthetic input files outside Workflow."""
import json
from pathlib import Path
import yaml
from chain.common.source_contracts import validate_initial

class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML without silent duplicate-key overwrite or implicit merge behavior."""

def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise ValueError("YAML merge keys are unsupported; declare fields explicitly")
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ValueError("YAML mapping keys must be strings")
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result

_UniqueKeyLoader.add_constructor("tag:yaml.org,2002:map", _unique_mapping)


def read_yaml(path):
    return yaml.load(path.read_text(encoding="utf8"), Loader=_UniqueKeyLoader)

def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"Duplicate JSON key: {key}")
        obj[key] = value
    return obj

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf8"), object_pairs_hook=_unique_pairs)

def load_initial(path):
    """Read only the requested initial file, with a closed eight-field schema."""
    obj = read_json(path)
    validate_initial(obj)
    return obj
