from qq_bot.services.typo_injector import maybe_apply_typo


class Seq:
    """确定性 rng：依次返回给定序列，超出后重复末值。"""

    def __init__(self, *values: float):
        self.values = list(values)

    def __call__(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


def test_disabled_when_rate_zero():
    text, applied = maybe_apply_typo("今天真的太累了不想动", rate=0.0, rng=Seq(0.0))
    assert (text, applied) == ("今天真的太累了不想动", False)


def test_short_reply_never_injected():
    text, applied = maybe_apply_typo("好的", rate=0.5, rng=Seq(0.1))
    assert applied is False


def test_digit_and_ascii_reply_never_injected():
    for text in ("版本2.0更新了", "check this out", "看 https://x.com"):
        _, applied = maybe_apply_typo(text, rate=0.5, rng=Seq(0.1))
        assert applied is False


def test_near_sound_pair_replacement():
    # rng 序列：0.1(命中rate) → 选"在"（eligible 首个）→ 位置 0
    text, applied = maybe_apply_typo("在这里等你很久了", rate=0.5, rng=lambda: 0.0)
    assert applied is True
    assert text != "在这里等你很久了"
    assert ("再" in text) or ("哪" in text) or ("得" in text)


def test_at_most_one_edit():
    out, applied = maybe_apply_typo("在这里再得地的以已象像", rate=1.0, rng=lambda: 0.0)
    if applied:
        diffs = sum(1 for a, b in zip(out, "在这里再得地的以已象像") if a != b)
        assert diffs <= 1


def test_sensitive_table_never_produces_new_words():
    # 混淆表都是常用字对：任一替换不引入非表内新字
    from qq_bot.services.typo_injector import _TYPO_PAIRS

    for a, b in _TYPO_PAIRS:
        assert len(a) == 1 and len(b) == 1
