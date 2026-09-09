"""Explicit-path, strict standalone protocol YAML loading."""

from pathlib import Path

import yaml
from pydantic import TypeAdapter, ValidationError

from relay.core.protocols import ProtocolDefinition


class ProtocolLoadError(ValueError):
    """Invalid protocol file, including field locations where available."""


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in mapping:
                    raise ProtocolLoadError(f"duplicate YAML key {key!r} at {key_node.start_mark}")
                mapping[key] = self.construct_object(value_node, deep=deep)
            except TypeError as exc:
                raise ProtocolLoadError(f"invalid YAML key at {key_node.start_mark}") from exc
        return mapping


def load_protocol(path: str | Path) -> ProtocolDefinition:
    try:
        with Path(path).open(encoding="utf-8") as stream:
            data = yaml.load(stream, Loader=_UniqueLoader)
        return TypeAdapter(ProtocolDefinition).validate_python(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ProtocolLoadError(f"{path}: {exc}") from exc
