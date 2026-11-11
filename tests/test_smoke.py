from carhunt import cli


def test_cli_entry_exists():
    assert cli.main([]) == 1
