"""Built-in polish prompt templates and seed helpers."""
import copy


def default_prompt_templates() -> list[dict]:
    """Factory list for first-run seed and 「恢复默认」.

    内置模板使用稳定 ID：首次 seed 与后续「恢复默认模板」产生相同身份，
    避免脏状态检查（_prompt_data_differs_from_disk 对比 ID 列表）误报
    未保存。用户自建条目仍用 uuid.uuid4().hex[:8]，与 __tpl_ 前缀不冲突。
    """
    return [
        {
            "id": "__tpl_translate_en",
            "name": "翻译为英语",
            "content": "翻译为英语",
        },
        {
            "id": "__tpl_dedup",
            "name": "删除重复",
            "content": "删除语句中的重复，去口语化",
        },
    ]


def seed_default_prompt_templates(cfg) -> None:
    """Populate empty custom_prompts with defaults and activate the first entry."""
    tpls = default_prompt_templates()
    cfg.custom_prompts = copy.deepcopy(tpls)
    cfg.active_prompt_id = tpls[0]["id"]
