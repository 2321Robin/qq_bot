from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg

from qq_bot.config import get_settings
from qq_bot.services.roco_pet_cards import pet_card_path
from qq_bot.services.roco_pets import PetRecord, find_pet, format_pet_query_result, format_pet_record, get_pet_records
from qq_bot.services.roco_skills import find_skills, format_skill_query_result, get_skill_records


roco_pet_command = on_command("精灵", aliases={"洛克"}, priority=5, block=True)
roco_skill_command = on_command("技能", priority=5, block=True)
roco_mention_pet = on_message(priority=10, block=False)


@roco_pet_command.handle()
async def handle_roco_pet(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    query = args.extract_plain_text().strip()
    record = find_pet(get_pet_records(), query)
    if record is None:
        await roco_pet_command.finish(format_pet_query_result(query, get_pet_records()))
    await roco_pet_command.finish(_format_pet_card_message(record))


@roco_skill_command.handle()
async def handle_roco_skill(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    query = args.extract_plain_text().strip()
    await roco_skill_command.finish(format_skill_query_result(query, get_skill_records()))


@roco_mention_pet.handle()
async def handle_roco_mention_pet(event: GroupMessageEvent) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    if not event.is_tome():
        return

    query = event.get_message().extract_plain_text().strip()
    record = find_pet(get_pet_records(), query)
    if record is not None:
        await roco_mention_pet.finish(_format_pet_card_message(record))

    if find_skills(get_skill_records(), query):
        await roco_mention_pet.finish(format_skill_query_result(query, get_skill_records()))


def _format_pet_card_message(record: PetRecord) -> MessageSegment | str:
    path = pet_card_path(record)
    if not path.exists():
        return format_pet_record(record)
    return MessageSegment.image(f"file:///{path.resolve().as_posix()}")
