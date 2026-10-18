from qq_bot.services.expression_learner import split_slots, validate_expression


class TestSplitSlots:
    def test_slot_and_fragments(self):
        fragments, count = split_slots("那{对象}吗")
        assert count == 1
        assert "".join(fragments) == "那吗"

    def test_no_slots(self):
        fragments, count = split_slots("确实")
        assert count == 0 and fragments == ["确实"]


class TestValidateExpression:
    def test_slotted_template_ok(self):
        ok, reason = validate_expression(
            template="那{对象}吗", evidence_quote="那新活动吗", evidence_text="那新活动吗 冲"
        )
        assert (ok, reason) == (True, "ok")

    def test_unslotted_short_word_ok(self):
        ok, reason = validate_expression(
            template="确实", evidence_quote="确实", evidence_text="确实 哈哈"
        )
        assert (ok, reason) == (True, "ok")

    def test_unslotted_over_six_rejected(self):
        ok, reason = validate_expression(
            template="这个真的太离谱了",
            evidence_quote="这个真的太离谱了",
            evidence_text="这个真的太离谱了",
        )
        assert (ok, reason) == (False, "unslotted_too_long")

    def test_fixed_content_word_rejected_for_slotted(self):
        ok, reason = validate_expression(
            template="{对象}杀我", evidence_quote="雾克杀我", evidence_text="雾克杀我"
        )
        # "杀" 不在虚词白名单 → 含槽位模板的固定片段带内容词
        assert (ok, reason) == (False, "fixed_content")

    def test_fixed_not_grounded_rejected(self):
        ok, reason = validate_expression(
            template="那{对象}吗", evidence_quote="冲冲冲", evidence_text="冲冲冲"
        )
        assert (ok, reason) == (False, "fixed_not_grounded")

    def test_bad_slot_rejected(self):
        ok, reason = validate_expression(
            template="{东西}吗", evidence_quote="冲吗", evidence_text="冲吗"
        )
        assert (ok, reason) == (False, "bad_slot")

    def test_strategy_rejected(self):
        ok, reason = validate_expression(
            template="别理他们", evidence_quote="别理他们", evidence_text="别理他们"
        )
        assert (ok, reason) == (False, "strategy")

    def test_qq_number_rejected(self):
        ok, reason = validate_expression(
            template="u1654321", evidence_quote="u1654321", evidence_text="u1654321"
        )
        assert (ok, reason) == (False, "forbidden")

    def test_too_long_rejected(self):
        ok, reason = validate_expression(
            template="很" * 21, evidence_quote="很" * 21, evidence_text="很" * 21
        )
        assert (ok, reason) == (False, "too_long")

    def test_empty_rejected(self):
        assert validate_expression(template="  ", evidence_quote="x", evidence_text="x") == (
            False,
            "empty",
        )
