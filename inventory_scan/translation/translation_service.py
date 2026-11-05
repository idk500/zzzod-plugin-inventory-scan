import difflib
import json
import os
from typing import Dict, Optional, Tuple

from one_dragon.utils import os_utils
from one_dragon.utils.log_utils import log


class TranslationService:
    """翻译服务"""

    def __init__(self):
        # 从 assets/wiki_data 读取字典
        self.user_dict_path = os.path.join(
            os_utils.get_path_under_work_dir('assets', 'wiki_data'),
            'zzz_translation.json'
        )
        # 本项目只有一份字典：config/zzz_translation.json
        # “自带的默认字典”和“下载更新的字典”是同一个文件路径，只是我们预置了一份初始内容。
        self.translation_dict: Optional[Dict] = None

        # 特殊名称映射（繁简转换等）
        # 翻译表未收录的新角色：中文名直接映射到分析器使用的角色 code。
        # 表数据 last_updated 2026-02-05，3.x 新角色不在其中（蕾米埃尔/佩洛伊斯等），
        # 缺映射时模糊匹配会把它们错配到字符相近的旧角色（实测：蕾米埃尔→Koleda）。
        self.name_to_code_override = {
            '蕾米埃尔': 'Remielle',
            '佩洛伊斯': 'Pyrois',
        }

        self.special_name_mapping = {
            '賽斯': '赛斯',  # 繁体 -> 简体
            '賽斯・洛威尔': '赛斯',  # 繁体 -> 简体
            '赛斯・洛威尔': '赛斯',  # 简体 -> 简体
            '搖摆': '摇摆',  # 错别字修复
            '昇常': '异常',  # 错别字修复
            '異常': '异常',  # 日繁体 -> 简体
            '昇常精通': '异常精通',  # 错别字修复
            '昇常掌控': '异常掌控',  # 错别字修复
        }

        self._load_dict()

    def _load_dict(self):
        """加载翻译字典"""
        # 1. 尝试加载用户字典
        if self._try_load_dict(self.user_dict_path, "用户"):
            return

        # 2. 加载失败，使用空字典
        log.warning("未找到任何翻译字典，使用空字典")
        self.translation_dict = {
            'character': {},
            'weapon': {},
            'equipment': {}
        }

    def _try_load_dict(self, path: str, type_name: str) -> bool:
        """尝试加载字典"""
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    self.translation_dict = json.load(f)
                log.info(f"加载{type_name}翻译字典成功: {path}")
                return True
        except Exception as e:
            log.error(f"加载{type_name}翻译字典失败: {e}")
        return False

    def translate_character(self, name: str, target_lang: str = 'CHS') -> str:
        """翻译角色名称"""
        return self._translate('character', name, target_lang)

    def translate_weapon(self, name: str, target_lang: str = 'CHS') -> str:
        """翻译音擎名称"""
        return self._translate('weapon', name, target_lang)

    def translate_equipment(self, name: str, target_lang: str = 'CHS') -> str:
        """翻译驱动盘名称"""
        return self._translate('equipment', name, target_lang)

    def correct_text(self, text: str) -> str:
        """
        修正OCR文本中的常见错误
        该方法会对文本进行遍历替换，适用于包含错别字的长文本
        """
        if not text:
            return text

        result = text
        for wrong, right in self.special_name_mapping.items():
            if wrong in result:
                result = result.replace(wrong, right)

        return result

    def _translate(self, category: str, name: str, target_lang: str) -> str:
        """通用翻译方法，支持模糊匹配"""
        if not self.translation_dict:
            return name

        # 0. 翻译表未收录的新角色，直接给 code（否则会走模糊匹配错配）
        if category == 'character' and name in self.name_to_code_override:
            code = self.name_to_code_override[name]
            log.info(f'新角色映射: {name} -> {code}')
            return code

        # 0.1 检查特殊名称映射（优先级次之）
        if name in self.special_name_mapping:
            mapped_name = self.special_name_mapping[name]
            log.info(f"特殊名称映射: {name} -> {mapped_name}")
            name = mapped_name

        category_dict = self.translation_dict.get(category, {})

        # 辅助函数：从新格式中提取翻译结果
        def extract_translation(trans_obj: dict, lang: str, default: str) -> str:
            """从翻译对象中提取文本（新格式：{"EN": {"name": "...", ...}}）"""
            result = trans_obj.get(lang, default)
            if isinstance(result, dict):
                result = result.get('name', default)
            return result

        # 1. 尝试直接匹配（完全匹配）
        if name in category_dict:
            return extract_translation(category_dict[name], target_lang, name)

        # 2. 尝试从其他语言反向查找（完全匹配）
        for key, translations in category_dict.items():
            if not isinstance(translations, dict):
                continue

            for lang, trans_name in translations.items():
                # 新格式：trans_name 是字典 {"name": "...", ...}
                if isinstance(trans_name, dict):
                    trans_name = trans_name.get('name', '')
                
                if trans_name == name:
                    return extract_translation(translations, target_lang, name)

        # 3. 模糊匹配（仅用于修正 OCR 误读，阈值与长度都收紧）
        best_match, confidence = self._fuzzy_match(name, category_dict)
        if best_match and confidence >= 0.5:
            log.info(f"模糊匹配: {name} -> {best_match} (置信度: {confidence:.4f})")
            return extract_translation(category_dict[best_match], target_lang, name)
        else:
            log.warning(f"模糊匹配失败: {name} (最佳匹配: {best_match}, 置信度: {confidence:.4f})")

        # 4. 未匹配到，返回原名
        log.warning(f"未找到匹配: {name}")
        return name

    def _fuzzy_match(self, name: str, category_dict: Dict) -> Tuple[Optional[str], float]:
        """
        模糊匹配，返回最佳匹配的key和置信度

        Args:
            name: 待匹配的名称
            category_dict: 类别字典

        Returns:
            (best_match_key, confidence)
        """
        best_match = None
        best_ratio = 0.0

        # 只比较中文名与英文名。翻译表里还有 desc / 皮肤描述等长文本，
        # 一并参与比较会让短名字（如「雅」）在长描述里意外拿到高分。
        candidates = []
        for key, translations in category_dict.items():
            if not isinstance(translations, dict):
                continue
            for lang in ('CHS', 'EN'):
                value = translations.get(lang)
                if isinstance(value, str) and value:
                    candidates.append((key, value))

        # 计算相似度
        for key, candidate in candidates:
            # 长度差超过 1 的不可能是同名的 OCR 误读，直接跳过（避免「佩洛伊斯」命中「赛斯」）
            if abs(len(candidate) - len(name)) > 1:
                continue
            ratio = difflib.SequenceMatcher(None, name, candidate).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = key

        return best_match, best_ratio

    def reload(self):
        """重新加载字典"""
        self._load_dict()


def __debug():
    """测试翻译"""
    service = TranslationService()

    # 测试角色翻译
    print(service.translate_character('Corin', 'CHS'))  # 应该输出: 可琳
    print(service.translate_character('可琳', 'EN'))    # 应该输出: Corin

    # 测试音擎翻译
    print(service.translate_weapon('The Brimstone', 'CHS'))  # 应该输出: 硫磺石

    # 测试驱动盘翻译
    print(service.translate_equipment('Proto Punk', 'CHS'))  # 应该输出: 原始朋克


if __name__ == '__main__':
    __debug()
