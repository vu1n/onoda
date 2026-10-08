"""The not-ready label for uncited jobs (A70): parser status only, never a reference."""


def test_uncited_jobs_are_labelled_not_ready_only_when_the_parser_saw_them_fail():
    from onoda.adapters.pi.vte_shadow import _failed

    ok = {"termination": "normal", "scf": "converged", "casscf_converged": True}
    assert not _failed(ok)
    assert not _failed({**ok, "casscf_converged": None})  # no CASSCF verdict is not a failure
    assert _failed({**ok, "termination": "error"})
    assert _failed({**ok, "termination": "absent"})
    assert _failed({**ok, "scf": "not_converged"})
    assert _failed({**ok, "casscf_converged": False})
