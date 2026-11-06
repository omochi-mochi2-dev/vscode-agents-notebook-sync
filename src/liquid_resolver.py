import logging
import re
from typing import Dict, Match, Pattern

logger = logging.getLogger(__name__)

LIQUID_VARIABLE_PATTERN: Pattern[str] = re.compile(
    r'\{%\s*data\s+variables\.([\w-]+)\.([\w-]+)\s*%\}'
)


def resolve_liquid_variables(content: str, variables: Dict[str, Dict[str, str]]) -> str:
    """
    本文中の {% data variables.<file>.<key> %} を variables[file][key] の値に置換する。
    未定義の変数は警告ログを出力し、元のタグを維持する。
    """
    def _replace(match: Match[str]) -> str:
        file_key: str = match.group(1)
        var_key: str = match.group(2)
        value = variables.get(file_key, {}).get(var_key)
        if value is None:
            logger.warning(f"Liquid variable not found: variables.{file_key}.{var_key}. Keeping original tag.")
            return match.group(0)
        return str(value)

    return LIQUID_VARIABLE_PATTERN.sub(_replace, content)

