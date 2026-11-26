import pytest

from yadro.router import Policy, Proposal, route


def P(**kw):
    base = dict(audience="internal", recipient_known=True, quotes_ok=True,
                commitments=(), needs_decision=False, confidence=0.9, attempt=1)
    base.update(kw)
    return Proposal(**base)


def test_internal_clean_goes_now():
    r = route(P())
    assert r.path == "now" and r.reasons == []


def test_external_clean_goes_undo():
    assert route(P(audience="external")).path == "undo"


def test_external_with_trust_goes_now():
    assert route(P(audience="external"), Policy(auto_external=True)).path == "now"


@pytest.mark.parametrize("kw,word", [
    (dict(needs_decision=True), "решени"),
    (dict(recipient_known=False), "получател"),
    (dict(commitments=("скидка 20 %",)), "скидка 20 %"),
    (dict(confidence=0.3), "уверен"),
])
def test_decision_triggers(kw, word):
    r = route(P(**kw))
    assert r.path == "decision"
    assert any(word in x for x in r.reasons)


def test_bad_quotes_first_attempt_revise():
    r = route(P(quotes_ok=False))
    assert r.path == "revise"
    assert any("цитат" in x for x in r.reasons)


def test_bad_quotes_second_attempt_decision():
    assert route(P(quotes_ok=False, attempt=2)).path == "decision"


def test_decision_outranks_revise():
    r = route(P(quotes_ok=False, needs_decision=True))
    assert r.path == "decision"


def test_all_reasons_collected():
    r = route(P(recipient_known=False, commitments=("бесплатная доставка",), confidence=0.1))
    assert len(r.reasons) == 3


def test_confidence_threshold_from_policy():
    assert route(P(confidence=0.5), Policy(min_confidence=0.4)).path == "now"
    assert route(P(confidence=0.5), Policy(min_confidence=0.6)).path == "decision"


def test_unknown_audience_rejected():
    with pytest.raises(ValueError):
        route(P(audience="everyone"))
