from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

plugin_dir = Path(__file__).parent / "src" / "qq_bot" / "plugins"
nonebot.load_plugins(str(plugin_dir))


if __name__ == "__main__":
    nonebot.run()
