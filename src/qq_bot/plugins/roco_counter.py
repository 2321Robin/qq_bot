from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.params import CommandArg

from qq_bot.config import get_settings
from qq_bot.services.onebot_send import finish_with_send_errors_logged
from qq_bot.services.roco_counter import (
    RocoCounterStore,
    format_capture_result,
    format_counter_summary,
    parse_counter_args,
)


roco_counter_command = on_command("计数", priority=5, block=True)


@roco_counter_command.handle()
async def handle_roco_counter(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    settings = get_settings()
    if not settings.group_allowed(event.group_id):
        return

    action = parse_counter_args(args.extract_plain_text())
    if action.error:
        await finish_with_send_errors_logged(roco_counter_command, action.error)

    store = RocoCounterStore(settings.resolved_roco_counter_path)
    if action.show_summary:
        rows = store.get_summary(
            group_id=event.group_id,
            user_id=event.user_id,
            season=settings.roco_counter_season,
        )
        shiny_indexes = store.get_summary_shiny_indexes(
            group_id=event.group_id,
            user_id=event.user_id,
            season=settings.roco_counter_season,
        )
        await finish_with_send_errors_logged(
            roco_counter_command,
            format_counter_summary(
                season=settings.roco_counter_season,
                rows=rows,
                shiny_indexes=shiny_indexes,
            ),
        )

    row = store.add_capture(
        group_id=event.group_id,
        user_id=event.user_id,
        season=settings.roco_counter_season,
        pet_name=action.pet_name,
        shiny=action.shiny,
    )
    rows = store.get_summary(
        group_id=event.group_id,
        user_id=event.user_id,
        season=settings.roco_counter_season,
    )
    shiny_indexes = store.get_shiny_indexes(
        group_id=event.group_id,
        user_id=event.user_id,
        season=settings.roco_counter_season,
        pet_name=action.pet_name,
    )
    await finish_with_send_errors_logged(
        roco_counter_command,
        format_capture_result(
            season=settings.roco_counter_season,
            row=row,
            rows=rows,
            shiny=action.shiny,
            shiny_index=shiny_indexes[-1] if action.shiny and shiny_indexes else None,
        ),
    )
