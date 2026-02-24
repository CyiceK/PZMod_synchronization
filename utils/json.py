# json5_utils.py
import json5
from json5.loader import ModelLoader
from json5.dumper import ModelDumper, modelize
from json5.model import JSONObject, JSONArray, JSONText, BlockComment


class JSON5Editor:
    """JSON5 reader/writer with comment preservation."""

    def __init__(self, file_path):
        self.file_path = file_path
        self._model = None  # Internal data structure model

    def load(self):
        """Load a JSON5 file and parse it into an editable object."""
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                self._model = json5.load(f, loader=ModelLoader())
                self._enhance_model()  # Enhance object operations
            return self
        except FileNotFoundError:
            raise ValueError(f"文件 {self.file_path} 不存在")
        except json5.JSON5DecodeError as e:
            raise ValueError(f"JSON5解析错误: {e}")

    def save(self, indent=4):
        """Save the updated content to file."""
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json5.dump(self._model, f, dumper=ModelDumper(indent=indent))
        return self

    def _enhance_model(self):
        """Add dict-like operations to the model."""

        def _get_item(obj, key):
            if isinstance(obj, JSONObject):
                for kv in obj.key_value_pairs:
                    if kv.key.value == key:
                        return kv.value
            elif isinstance(obj, JSONArray):
                return obj.values[key]
            return None

        def _set_item(obj, key, value):
            if isinstance(obj, JSONObject):
                # Find and update or append a key/value pair
                for kv in obj.key_value_pairs:
                    if kv.key.value == key:
                        kv.value = modelize(value)
                        return
                obj.key_value_pairs.append(
                    json5.model.KeyValuePair(modelize(key), modelize(value))
                )
            elif isinstance(obj, JSONArray):
                obj.values[key] = modelize(value)

        # Dynamically add operator overloads
        self._model.__getitem__ = lambda k: _get_item(self._model, k)
        self._model.__setitem__ = lambda k, v: _set_item(self._model, k, v)
        return self

    def get(self, path, default=None):
        """Get a nested value by path (e.g. 'a.b[0].c')."""
        parts = path.replace('[', '.').replace(']', '').split('.')
        current = self._model
        for part in parts:
            if isinstance(current, (JSONObject, JSONArray)):
                current = current[part]
            else:
                return default
        return current.value if hasattr(current, 'value') else current

    def add_comment(self, path, comment):
        """Add a block comment for the given path."""
        node = self.get(path)
        if node and hasattr(node, 'wsc_before'):
            node.wsc_before.append(BlockComment(f"/* {comment} */"))
        return self

    @property
    def data(self):
        """Return raw dict data (comments are lost)."""
        return json5.dumps(self._model, dumper=ModelDumper())
