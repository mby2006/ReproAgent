import pytest
from reproagent_paperparse import QuoteLocator, ground_card, normalize, parse_pdf
from reproagent_schemas import ParsedDocument, ReproductionCard, TableData, TextBlock


@pytest.fixture
def document() -> ParsedDocument:
    return ParsedDocument(
        num_pages=9,
        blocks=[
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
    def test_llm_page_claim_overridden(self, document):
        card = {
            "optimizer": field("sgd", quote="Layer C3 has 16 feature maps.", page=99),
        }
        grounded = ground_card(card, document)
        assert grounded["optimizer"]["source_page"] == 5
        assert grounded["optimizer"]["source_section"] == "II. METHOD"
        assert grounded["optimizer"]["human_confirmed"] is False

    def test_unverifiable_quote_demoted_to_inferred(self, document):
        card = {"optimizer": field("sgd", quote="We use SGD with momentum 0.9.")}
        grounded = ground_card(card, document)
        f = grounded["optimizer"]
        assert f["status"] == "inferred"
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
        assert grounded["hyperparameters"]["batch_size"]["status"] == "inferred"
        assert grounded["reported_results"][0]["source_page"] == 7
        assert grounded["reported_results"][0]["metric"] == "error_rate"

    def test_grounded_card_validates(self, document):
        card = {
            "method": field("LeNet-5", quote="Layer C3 has 16 feature maps."),
            "model_architecture": field("conv net", quote="Layer C3 has 16 feature maps."),
            "dataset": field("MNIST", quote="The error rate on the MNIST test set is 0.8 percent."),
            "optimizer": field("sgd", quote="unfindable quote"),
            "loss_function": {"value": None, "status": "missing"},
        }
        grounded = ground_card(card, document)
        model = ReproductionCard.model_validate(grounded)
        assert model.method.source_page == 5
        assert model.optimizer.status.value == "inferred"

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
