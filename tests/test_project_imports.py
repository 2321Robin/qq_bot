def test_package_imports() -> None:
    import qq_bot

    assert qq_bot.__doc__ == "Custom QQ group bot package."
