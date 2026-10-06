import pytest

from evog.agents.answer_contract import answer_draft, parse_final_message, validate_final_message


def test_text_protocol_supports_multiline_answers_and_bias():
    raw = (
        "FINAL ANSWER: 第一行\n第二行\n"
        "CONFIDENCE: 0.4\n"
        "ANSWER BIAS: "
        + "我检索了授权范围内的记录，找到了部分相关证据，但仍缺少能够确认最终更新时间的消息，"
        "因此答案可能遗漏后续变化，限制来自检索覆盖不足；我也没有找到可以交叉验证该结论的第二条独立记录。"
    )
    parsed = parse_final_message(raw)
    assert parsed["final_answer"] == "第一行\n第二行"
    assert parsed["confidence"] == 0.4
    assert not validate_final_message(raw)
    assert answer_draft(raw).status == "partial"


@pytest.mark.parametrize(
    "raw",
    [
        "CONFIDENCE: 0.9",
        "FINAL ANSWER: answer",
        "FINAL ANSWER: answer\nCONFIDENCE: 1.2",
        "FINAL ANSWER: answer\nCONFIDENCE: 0.2\nANSWER BIAS: too short",
    ],
)
def test_text_protocol_rejects_incomplete_or_unsafe_messages(raw):
    assert validate_final_message(raw)
    with pytest.raises(ValueError):
        answer_draft(raw)
