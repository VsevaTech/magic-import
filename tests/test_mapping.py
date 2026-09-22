from app.services.ai.base import AISuggestion, NullProvider, mask_value, masked_samples
from app.services.ai.gemini import GeminiProvider
from app.services.mapping_engine import (
    MappingEngine,
    SourceColumn,
    TargetField,
    find_conflicts,
    normalize_header,
    template_match_score,
)

TARGETS = [
    TargetField(
        "customer_id", "Customer ID", "string", aliases=["id", "customer ref", "client id"]
    ),
    TargetField(
        "customer_name", "Customer name", "string", aliases=["name", "client", "full name"]
    ),
    TargetField(
        "email", "Email", "email", aliases=["mail", "e-mail", "email address", "e-mail address"]
    ),
    TargetField("phone", "Phone", "phone", aliases=["mobile", "mobile no", "telephone", "tel"]),
    TargetField(
        "company", "Company", "string", aliases=["organization", "business", "company name"]
    ),
    TargetField(
        "country", "Country", "country", aliases=["country name", "country code", "location"]
    ),
    TargetField("created_at", "Created at", "date", aliases=["created", "joined", "signup date"]),
]


def _by_source(suggestions):
    return {s.source_column: s for s in suggestions}


def test_normalize_header():
    assert normalize_header("E-mail Address") == "emailaddress"
    assert normalize_header("email_address") == "emailaddress"
    assert normalize_header("  Mobile No. ") == "mobile"  # noise word "no" removed
    assert normalize_header("Country_Name") == "countryname"


def test_exact_and_normalized_levels():
    engine = MappingEngine()
    out = _by_source(
        engine.suggest(
            [SourceColumn("email"), SourceColumn("Customer Name"), SourceColumn("created_at")],
            TARGETS,
        )
    )
    assert out["email"].target_field == "email" and out["email"].method == "exact"
    assert (
        out["Customer Name"].target_field == "customer_name"
        and out["Customer Name"].confidence == "HIGH"
    )
    assert out["created_at"].method == "exact"


def test_alias_level():
    engine = MappingEngine()
    out = _by_source(
        engine.suggest(
            [
                SourceColumn("Mobile No."),
                SourceColumn("E-mail Address"),
                SourceColumn("Organization"),
            ],
            TARGETS,
        )
    )
    assert out["Mobile No."].target_field == "phone" and out["Mobile No."].method == "alias"
    assert (
        out["E-mail Address"].target_field == "email" and out["E-mail Address"].confidence == "HIGH"
    )
    assert out["Organization"].target_field == "company"


def test_fuzzy_level_gives_medium_or_low():
    engine = MappingEngine()
    out = _by_source(
        engine.suggest([SourceColumn("Customer Nme"), SourceColumn("Cmpany Name")], TARGETS)
    )
    assert out["Customer Nme"].target_field == "customer_name"
    assert (
        out["Customer Nme"].confidence in ("MEDIUM", "LOW")
        and out["Customer Nme"].method == "fuzzy"
    )
    assert out["Cmpany Name"].target_field == "company"


def test_type_inference_level():
    engine = MappingEngine()
    out = _by_source(
        engine.suggest(
            [SourceColumn("Contact", detected_type="email", samples=["a@b.com"])], TARGETS
        )
    )
    assert (
        out["Contact"].target_field == "email"
        and out["Contact"].confidence == "LOW"
        and out["Contact"].method == "type"
    )


def test_saved_mapping_has_priority():
    engine = MappingEngine()
    memory = {normalize_header("Cust Ref"): "customer_id"}
    out = _by_source(engine.suggest([SourceColumn("Cust Ref")], TARGETS, memory=memory))
    assert out["Cust Ref"].target_field == "customer_id" and out["Cust Ref"].method == "saved"


def test_unmapped_column():
    engine = MappingEngine()
    out = _by_source(engine.suggest([SourceColumn("Notes"), SourceColumn("Client")], TARGETS))
    assert out["Notes"].target_field is None and out["Notes"].confidence == "UNMAPPED"
    assert out["Client"].target_field == "customer_name"


def test_fuzzy_does_not_double_book_a_target():
    engine = MappingEngine()
    out = engine.suggest([SourceColumn("Company Nam"), SourceColumn("Compny")], TARGETS)
    targets = [s.target_field for s in out if s.target_field == "company"]
    assert len(targets) == 1


def test_find_conflicts():
    conflicts = find_conflicts(
        {"Client": "customer_name", "Organization": "customer_name", "Mail": "email"}, TARGETS
    )
    assert conflicts == [{"target_field": "customer_name", "sources": ["Client", "Organization"]}]
    multi = [TargetField("tags", allow_multiple_sources=True)]
    assert find_conflicts({"a": "tags", "b": "tags"}, multi) == []


def test_template_match_score():
    cols = ["Client", "E-mail Address", "Mobile No.", "Organization"]
    assert template_match_score(cols, cols) == 1.0
    assert (
        template_match_score(cols, ["client", "e_mail address", "MOBILE NO", "Organization"]) == 1.0
    )
    assert template_match_score(cols, ["totally", "different"]) == 0.0


# ----------------------------------------------------------------------------- AI (mocked)
class FakeAI:
    name = "fake"
    available = True

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def suggest(self, sources, targets):
        self.calls.append(([s.name for s in sources], [t.name for t in targets]))
        return self.answers


def test_ai_used_only_for_unresolved_columns():
    ai = FakeAI({"Zzz Field": AISuggestion("email", "high")})
    engine = MappingEngine(ai)
    out = _by_source(
        engine.suggest([SourceColumn("Client"), SourceColumn("Zzz Field")], TARGETS, use_ai=True)
    )
    assert out["Zzz Field"].target_field == "email" and out["Zzz Field"].method == "ai"
    assert out["Zzz Field"].confidence == "MEDIUM"
    assert ai.calls == [(["Zzz Field"], [t.name for t in TARGETS if t.name != "customer_name"])]


def test_ai_disabled_or_unavailable_keeps_unmapped():
    ai = FakeAI({"Zzz Field": AISuggestion("email", "high")})
    out = _by_source(MappingEngine(ai).suggest([SourceColumn("Zzz Field")], TARGETS, use_ai=False))
    assert out["Zzz Field"].confidence == "UNMAPPED"
    out = _by_source(
        MappingEngine(NullProvider()).suggest([SourceColumn("Zzz Field")], TARGETS, use_ai=True)
    )
    assert out["Zzz Field"].confidence == "UNMAPPED"


def test_ai_failure_never_breaks_mapping():
    class Boom(FakeAI):
        def suggest(self, sources, targets):
            raise RuntimeError("quota")

    out = _by_source(MappingEngine(Boom({})).suggest([SourceColumn("Zzz")], TARGETS, use_ai=True))
    assert out["Zzz"].confidence == "UNMAPPED"


def test_ai_cannot_invent_targets():
    ai = FakeAI({"Zzz": AISuggestion("not_a_field", "high")})
    out = _by_source(MappingEngine(ai).suggest([SourceColumn("Zzz")], TARGETS, use_ai=True))
    assert out["Zzz"].target_field is None


def test_gemini_prompt_masks_samples_and_parses_response():
    provider = GeminiProvider(api_key="test", model="gemini-test")
    prompt = provider.build_prompt(
        [SourceColumn("Zzz Field", "email", ["dana.levi@example.com", "+972525551234"])],
        TARGETS,
    )
    assert "dana.levi@example.com" not in prompt and "d***@e***.com" in prompt
    assert "+972525551234" not in prompt and "+999999999999" in prompt
    text = (
        '```json\n{"mappings":[{"source":"Zzz Field","target":"email","confidence":"high"},'
        '{"source":"X","target":"bogus"}]}\n```'
    )
    resp = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    parsed = GeminiProvider.parse_response(resp, {t.name for t in TARGETS})
    assert parsed["Zzz Field"].target_field == "email" and parsed["Zzz Field"].confidence == "high"
    assert parsed["X"].target_field is None
    assert GeminiProvider.parse_response({"candidates": []}, set()) == {}


def test_gemini_http_failure_returns_empty(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", boom)
    provider = GeminiProvider(api_key="test")
    assert provider.suggest([SourceColumn("X")], TARGETS) == {}


def test_masking():
    assert mask_value("dana.levi@example.com") == "d***@e***.com"
    assert mask_value("052-555-1234") == "999-999-9999"
    assert mask_value("A very long free text value") == "A ***"
    assert mask_value("IL") == "IL"
    assert masked_samples(["a@b.com", "a@b.com", "c@d.org"]) == ["a***@b***.com", "c***@d***.org"]
