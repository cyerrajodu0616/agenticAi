from assistant.redact import redact


def test_redacts_ssn_email_phone():
    text = "John (SSN 123-45-6789, john.d@corp.com, 555-867-5309) asked about ARCF25344h646"
    red, mapping = redact(text)
    assert "123-45-6789" not in red
    assert "john.d@corp.com" not in red
    assert "555-867-5309" not in red
    assert "ARCF25344h646" in red  # business ids must survive redaction
    assert mapping["[REDACTED_SSN_1]"] == "123-45-6789"
    assert set(mapping.values()) == {"123-45-6789", "john.d@corp.com", "555-867-5309"}


def test_numbering_multiple_of_same_kind():
    red, mapping = redact("a@x.com and b@y.com")
    assert "[REDACTED_EMAIL_1]" in red and "[REDACTED_EMAIL_2]" in red
    assert len(mapping) == 2


def test_clean_text_untouched():
    red, mapping = redact("Where is the eConsent PDF for ARCF25344h697?")
    assert red == "Where is the eConsent PDF for ARCF25344h697?"
    assert mapping == {}
