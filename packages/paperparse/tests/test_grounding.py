import pytest
from reproagent_paperparse import QuoteLocator, ground_card, normalize, parse_pdf
from reproagent_schemas import ParsedDocument, ReproductionCard, TableData, TextBlock


@pytest.fixture
def document() -> ParsedDocument:
    return ParsedDocument(
        num_pages=9,
        blocks=[
            TextBlock(
                id="p2-b0",
                text="The LeNet-5 convolutional network is trained on the MNIST dataset.",
                page=2,
                section="I. INTRODUCTION",
            ),
            TextBlock(
                id="p5-b0",
                text="The learning rate is set to 0.001 for the ﬁrst two epochs.",
                page=5,
                section="II. METHOD",
            ),
            TextBlock(
                id="p5-b1",
                text="Layer C3 has 16 feature maps.",
                page=5,
                section="II. METHOD",
            ),
            TextBlock(
                id="p5-b2",
                text="The network is trained with the SGD optimizer.",
                page=5,
                section="II. METHOD",
            ),
            TextBlock(
                id="p7-b0",
                text="The error rate on the MNIST test set is 0.8 percent.",
                page=7,
                section="III. RESULTS",
            ),
        ],
        tables=[TableData(id="p9-t0", page=9, section="III. RESULTS", rows=[["LeNet-5", "0.8"]])],
    )


def field(value="x", quote=None, status="paper_explicit", page=99, **kw) -> dict:
    return {
        "value": value,
        "status": status,
        "source_page": page,
        "source_section": "LLM GUESS",
        "evidence_quote": quote,
        "confidence": 0.9,
        "human_confirmed": True,
        **kw,
    }


class TestQuoteLocator:
    def test_exact_match_returns_parser_page(self, document):
        match = QuoteLocator(document).locate("Layer C3 has 16 feature maps.")
        assert match is not None
        assert (match.page, match.section, match.block_id) == (5, "II. METHOD", "p5-b1")

    def test_ligature_and_case_normalized(self, document):
        match = QuoteLocator(document).locate("the FIRST two epochs")
        assert match is not None
        assert match.page == 5

    def test_cross_block_quote_same_page(self, document):
        quote = (
            "The learning rate is set to 0.001 for the first two epochs. "
            "Layer C3 has 16 feature maps."
        )
        match = QuoteLocator(document).locate(quote)
        assert match is not None
        assert match.page == 5
        assert match.block_id == "p5-b0"

    def test_table_cell_quote(self, document):
        match = QuoteLocator(document).locate("LeNet-5 0.8")
        assert match is not None
        assert match.page == 9

    def test_fuzzy_minor_word_change(self, document):
        match = QuoteLocator(document).locate(
            "The learning rate was set to 0.001 for the first two epochs."
        )
        assert match is not None
        assert match.page == 5
        assert match.score < 1.0

    def test_fabricated_quote_not_located(self, document):
        assert QuoteLocator(document).locate("The batch size is 256 on page 12.") is None

    def test_too_short_quote_rejected(self, document):
        assert QuoteLocator(document).locate("0.8") is None

    def test_normalize(self):
        assert normalize("The ﬁrst  32×32\nimage!") == "the first 32 32 image"


class TestGroundCard:
    def test_fabricated_quote_is_citation_not_found(self, document):
        card = {"optimizer": field("sgd", quote="We use a magical optimizer.", page=99)}
        result = ground_card(card, document)
        f = result["optimizer"]
        assert f["grounding_status"] == "citation_not_found"
        assert f["evidence_quote"] is None
        assert f["status"] == "inferred"
        assert f["source_page"] is None
        assert f["source_section"] is None
        assert f["candidate_quote"] == "We use a magical optimizer."
        assert f["candidate_page"] == 99
        assert f["grounding_reason"]
        assert f["human_confirmed"] is False
        assert f["confidence"] <= 0.4
        assert any(
            "optimizer" in m and "not accepted as paper evidence" in m
            for m in result["missing_information"]
        )

    def test_real_but_unrelated_quote_is_located_unsupported(self, document):
        card = {"optimizer": field("sgd", quote="Layer C3 has 16 feature maps.", page=99)}
        result = ground_card(card, document)
        f = result["optimizer"]
        assert f["grounding_status"] == "located_unsupported"
        assert f["source_page"] is None
        assert f["candidate_page"] == 5
        assert f["candidate_section"] == "II. METHOD"
        assert f["candidate_quote"] == "Layer C3 has 16 feature maps."
        assert f["evidence_quote"] is None
        assert f["status"] == "inferred"
        assert f["value"] == "sgd"
        assert f["grounding_reason"]
        assert any(
            "optimizer" in m and "not accepted as paper evidence" in m
            for m in result["missing_information"]
        )

    def test_located_and_supported_quote_is_verified(self, document):
        card = {
            "hyperparameters": {
                "learning_rate": field(
                    0.001,
                    quote="The learning rate is set to 0.001 for the first two epochs.",
                    page=99,
                )
            }
        }
        f = ground_card(card, document)["hyperparameters"]["learning_rate"]
        assert f["grounding_status"] == "verified"
        assert f["value"] == 0.001
        assert f["source_page"] == 5
        assert f["source_section"] == "II. METHOD"
        assert f["evidence_quote"] == (
            "The learning rate is set to 0.001 for the first two epochs."
        )
        assert f["status"] == "paper_explicit"
        assert f["candidate_page"] is None
        assert f["grounding_reason"] is None

    def test_llm_page_claim_never_kept_when_quote_rejected(self, document):
        card = {"optimizer": field("sgd", quote="Layer C3 has 16 feature maps.", page=99)}
        grounded = ground_card(card, document)
        assert grounded["optimizer"]["source_page"] is None
        assert grounded["optimizer"]["human_confirmed"] is False

    def test_verified_optimizer_keeps_parser_location(self, document):
        card = {"optimizer": field("sgd", quote="trained with the SGD optimizer", page=99)}
        f = ground_card(card, document)["optimizer"]
        assert f["grounding_status"] == "verified"
        assert f["source_page"] == 5
        assert f["status"] == "paper_explicit"

    def test_unverifiable_quote_demoted_to_inferred(self, document):
        card = {"optimizer": field("sgd", quote="We use SGD with momentum 0.9.")}
        grounded = ground_card(card, document)
        f = grounded["optimizer"]
        assert f["status"] == "inferred"
        assert f["grounding_status"] == "citation_not_found"
        assert f["value"] == "sgd"
        assert f["evidence_quote"] is None
        assert f["source_page"] is None
        assert f["confidence"] <= 0.4

    def test_unverifiable_no_value_demoted_to_missing(self, document):
        card = {"loss_function": field(None, quote="The loss is cross entropy.")}
        grounded = ground_card(card, document)
        f = grounded["loss_function"]
        assert f["status"] == "missing"
        assert f["value"] is None
        assert any("loss_function" in m for m in grounded["missing_information"])

    def test_missing_stays_missing(self, document):
        card = {"dataset": {"value": None, "status": "missing"}}
        grounded = ground_card(card, document)
        assert grounded["dataset"]["status"] == "missing"
        assert grounded["missing_information"] == []

    def test_explicit_without_quote_demoted(self, document):
        card = {"method": field("LeNet-5", quote=None)}
        grounded = ground_card(card, document)
        assert grounded["method"]["status"] == "inferred"

    def test_paper_described_kept_when_grounded(self, document):
        card = {
            "data_preprocessing": {
                "centering": field(
                    "centered", quote="Layer C3 has 16 feature maps.", status="paper_described"
                )
            }
        }
        grounded = ground_card(card, document)
        f = grounded["data_preprocessing"]["centering"]
        assert f["status"] == "paper_described"
        assert f["source_page"] == 5
        assert f["grounding_status"] == "verified"
        assert f["evidence_quote"] == "Layer C3 has 16 feature maps."

    def test_paper_described_ungrounded_demoted_to_inferred(self, document):
        card = {
            "data_preprocessing": {
                "centering": field("centered", quote="unfindable prose", status="paper_described")
            }
        }
        grounded = ground_card(card, document)
        f = grounded["data_preprocessing"]["centering"]
        assert f["status"] == "inferred"
        assert f["source_page"] is None

    def test_dict_and_list_fields_grounded(self, document):
        card = {
            "hyperparameters": {
                "learning_rate": field(0.001, quote="The learning rate is set to 0.001"),
                "batch_size": field(256, quote="batch size of 256"),
            },
            "reported_results": [
                {
                    **field(0.8, quote="The error rate on the MNIST test set is 0.8 percent."),
                    "metric": "error_rate",
                }
            ],
        }
        grounded = ground_card(card, document)
        assert grounded["hyperparameters"]["learning_rate"]["source_page"] == 5
        assert grounded["hyperparameters"]["learning_rate"]["grounding_status"] == "verified"
        assert grounded["hyperparameters"]["batch_size"]["status"] == "inferred"
        assert grounded["hyperparameters"]["batch_size"]["grounding_status"] == "citation_not_found"
        assert grounded["reported_results"][0]["source_page"] == 7
        assert grounded["reported_results"][0]["grounding_status"] == "verified"
        assert grounded["reported_results"][0]["metric"] == "error_rate"

    def test_grounded_card_validates(self, document):
        lenet_quote = "The LeNet-5 convolutional network is trained on the MNIST dataset."
        card = {
            "method": field("LeNet-5", quote=lenet_quote),
            "model_architecture": field("convolutional network", quote=lenet_quote),
            "dataset": field("MNIST", quote=lenet_quote),
            "optimizer": field("sgd", quote="unfindable quote"),
            "loss_function": {"value": None, "status": "missing"},
        }
        grounded = ground_card(card, document)
        model = ReproductionCard.model_validate(grounded)
        assert model.method.source_page == 2
        assert model.method.grounding_status.value == "verified"
        assert model.optimizer.status.value == "inferred"
        assert model.optimizer.grounding_status.value == "citation_not_found"
        assert model.optimizer.candidate_quote == "unfindable quote"
        assert model.loss_function.grounding_status.value == "not_checked"

    def test_end_to_end_with_parsed_pdf(self, paper_pdf):
        doc = parse_pdf(paper_pdf)
        card = {
            "hyperparameters": {
                "learning_rate": field(
                    0.001, quote="The learning rate is set to 0.001 for the first two epochs."
                )
            },
            "reported_results": [
                {
                    **field(0.8, quote="The error rate on the MNIST test set is 0.8 percent."),
                    "metric": "error_rate",
                }
            ],
        }
        grounded = ground_card(card, doc)
        assert grounded["hyperparameters"]["learning_rate"]["source_page"] == 2
        assert grounded["reported_results"][0]["source_page"] == 3
        lr = grounded["hyperparameters"]["learning_rate"]
        scalar = lr | {"value": "lenet-5"}
        ReproductionCard.model_validate(
            {
                "method": scalar,
                "model_architecture": scalar,
                "dataset": scalar,
                "optimizer": scalar,
                "loss_function": scalar,
                "hyperparameters": {"learning_rate": lr},
            }
        )
