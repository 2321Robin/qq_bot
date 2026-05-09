from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.params import CommandArg

from qq_bot.config import get_settings
from qq_bot.services.roco_pets import format_pet_query_result, get_pet_records


roco_pet_command = on_command("精灵", aliases={"洛克"}, priority=5, block=True)


@roco_pet_command.handle()
async def handle_roco_pet(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    query = args.extract_plain_text().strip()
    await roco_pet_command.finish(format_pet_query_result(query, get_pet_records()))
