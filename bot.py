from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from qq_bot.plugins.health import install_health_routes
from qq_bot.runtime import install_runtime_lifecycle

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# Install the application lifecycle (runtime resources + health routes)
# before plugins load, so plugins can only ever see an initialized runtime.
install_runtime_lifecycle(driver)
install_health_routes()

plugin_dir = Path(__file__).parent / "src" / "qq_bot" / "plugins"
nonebot.load_plugins(str(plugin_dir))


if __name__ == "__main__":
    nonebot.run()
