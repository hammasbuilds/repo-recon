def test_the_right_parameter_is_the_one_verified():
    """`_parameter_for_default` matched by line number, so with several defaults on one
    line the first parameter won for every proposal.

    `def f(a=[], b=[])` with `b.append(1)` retracted BOTH findings, with the reason
    "the default is never mutated inside f()" — a false statement about the code, and
    the genuine bug dropped. The mirror case confirmed two where one was real.
    """
    from reporecon.reviewbot.checks import propose
    from reporecon.reviewbot.verify import FileContext, verify

    for source, mutated in (
        ("def f(a=[], b=[]):\n    b.append(1)\n    return b\n", "b"),
        ("def g(a=[], b=[]):\n    a.append(1)\n    return a\n", "a"),
    ):
        context = FileContext.build("m.py", source)
        verdicts = [verify(context, p) for p in propose(source)]
        confirmed = [v for v in verdicts if v.confirmed]
        assert len(confirmed) == 1, [v.proposal.message for v in confirmed]
        assert f"for {mutated}=" in confirmed[0].proposal.message
        assert confirmed[0].proposal.evidence["parameter"] == mutated
