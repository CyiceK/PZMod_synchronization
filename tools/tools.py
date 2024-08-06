# -*- coding:utf-8 -*-
import chardet

from base.base_data import BaseData


class Tools(BaseData):
    def __init__(self):
        super().__init__()

    @staticmethod
    def detect_file_encoding(file_path) -> str:
        with open(file_path, 'rb') as f:
            raw_data = f.read()
            result = chardet.detect(raw_data)
            return result['encoding']
