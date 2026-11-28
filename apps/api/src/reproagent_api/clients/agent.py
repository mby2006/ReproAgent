import httpx
from reproagent_schemas import (
    CandidateRequest,
    CandidateResponse,
    ClassifyRequest,
    CodegenRequest,
    CodegenResponse,
    ComparisonPlanRequest,
    ComparisonPlanResponse,
    ComparisonReportRequest,
    ComparisonReportResponse,
    ErrorClassification,
    EvaluateRequest,
    EvaluateResponse,
    ExplainResponse,
    ExtractRequest,
    ExtractResponse,
    GroundRequest,
    GroundResponse,
    PaperSupportCheck,
    ParsedDocument,
    ParseRequest,
    ParseResponse,
    PlanRequest,
    PlanResponse,
    RecommendResponse,
    RepairRequest,
    RepairResponse,
    ReproductionCard,
    ReviewRequest,
    StaticValidateRequest,
    UnderstandRequest,
    UnderstandResponse,
    ValidationReport,
    VerifyPairRequest,
    VerifyPairResponse,
)

from ..config import ApiSettings


class AgentClient:
    """HTTP contract: API -> Agent. The API never runs agent logic in-process."""

    def __init__(self, settings: ApiSettings, transport: httpx.AsyncBaseTransport | None = None):
        # trust_env=False: some hosts set NO_PROXY entries (e.g. "[::1]") that
        # httpx cannot parse; agent traffic is local and never proxied.
        self._client = httpx.AsyncClient(
            base_url=settings.agent_base_url,
            timeout=30.0,
            transport=transport,
            trust_env=False,
        )

    async def understand(self, request: UnderstandRequest) -> UnderstandResponse:
        response = await self._client.post(
            "/v1/understand", json=request.model_dump(mode="json"), timeout=900.0
        )
        response.raise_for_status()
        return UnderstandResponse.model_validate(response.json())

    async def parse_pdf(self, request: ParseRequest) -> ParseResponse:
        response = await self._client.post(
            "/v1/parse", json=request.model_dump(mode="json"), timeout=300.0
        )
        response.raise_for_status()
        return ParseResponse.model_validate(response.json())

    async def extract_spec(self, request: ExtractRequest) -> ExtractResponse:
        response = await self._client.post(
            "/v1/extract", json=request.model_dump(mode="json"), timeout=900.0
        )
        response.raise_for_status()
        return ExtractResponse.model_validate(response.json())

    async def ground_spec(self, request: GroundRequest) -> GroundResponse:
        response = await self._client.post(
            "/v1/ground", json=request.model_dump(mode="json"), timeout=120.0
        )
        response.raise_for_status()
        return GroundResponse.model_validate(response.json())

    async def check_support(
        self, document: ParsedDocument, card: ReproductionCard | None
    ) -> PaperSupportCheck:
        payload = {
            "document": document.model_dump(mode="json"),
            "card": card.model_dump(mode="json") if card is not None else None,
        }
        response = await self._client.post("/v1/papers/support-check", json=payload, timeout=30.0)
        response.raise_for_status()
        data = response.json()
        return PaperSupportCheck(
            supported=data["supported"], code=data["code"], message=data["message"]
        )

    async def explain(self, request: ReviewRequest) -> ExplainResponse:
        response = await self._client.post(
            "/v1/explain", json=request.model_dump(mode="json", by_alias=True), timeout=120.0
        )
        response.raise_for_status()
        return ExplainResponse.model_validate(response.json())

    async def recommend(self, request: ReviewRequest) -> RecommendResponse:
        response = await self._client.post(
            "/v1/recommend", json=request.model_dump(mode="json", by_alias=True), timeout=120.0
        )
        response.raise_for_status()
        return RecommendResponse.model_validate(response.json())

    async def plan(self, request: PlanRequest) -> PlanResponse:
        response = await self._client.post(
            "/v1/plan", json=request.model_dump(mode="json"), timeout=60.0
        )
        response.raise_for_status()
        return PlanResponse.model_validate(response.json())

    async def codegen(self, request: CodegenRequest) -> CodegenResponse:
        response = await self._client.post(
            "/v1/codegen", json=request.model_dump(mode="json"), timeout=60.0
        )
        response.raise_for_status()
        return CodegenResponse.model_validate(response.json())

    async def validate_code(self, request: StaticValidateRequest) -> ValidationReport:
        response = await self._client.post(
            "/v1/codegen/validate", json=request.model_dump(mode="json"), timeout=60.0
        )
        response.raise_for_status()
        return ValidationReport.model_validate(response.json())

    async def classify_error(self, request: ClassifyRequest) -> ErrorClassification:
        response = await self._client.post(
            "/v1/codegen/classify", json=request.model_dump(mode="json"), timeout=30.0
        )
        response.raise_for_status()
        return ErrorClassification.model_validate(response.json())

    async def repair_code(self, request: RepairRequest) -> RepairResponse:
        response = await self._client.post(
            "/v1/codegen/repair", json=request.model_dump(mode="json"), timeout=60.0
        )
        response.raise_for_status()
        return RepairResponse.model_validate(response.json())

    async def evaluate(self, request: EvaluateRequest) -> EvaluateResponse:
        response = await self._client.post(
            "/v1/evaluate", json=request.model_dump(mode="json"), timeout=60.0
        )
        response.raise_for_status()
        return EvaluateResponse.model_validate(response.json())

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- paired comparison (deterministic agent endpoints) ---

    async def comparison_candidates(self, request: CandidateRequest) -> CandidateResponse:
        response = await self._client.post(
            "/v1/comparisons/candidates", json=request.model_dump(mode="json"), timeout=60.0
        )
        response.raise_for_status()
        return CandidateResponse.model_validate(response.json())

    async def comparison_plan(self, request: ComparisonPlanRequest) -> ComparisonPlanResponse:
        response = await self._client.post(
            "/v1/comparisons/plan", json=request.model_dump(mode="json"), timeout=60.0
        )
        response.raise_for_status()
        return ComparisonPlanResponse.model_validate(response.json())

    async def comparison_verify(self, request: VerifyPairRequest) -> VerifyPairResponse:
        response = await self._client.post(
            "/v1/comparisons/verify", json=request.model_dump(mode="json"), timeout=60.0
        )
        response.raise_for_status()
        return VerifyPairResponse.model_validate(response.json())

    async def comparison_report(
        self, request: ComparisonReportRequest
    ) -> ComparisonReportResponse:
        response = await self._client.post(
            "/v1/comparisons/report", json=request.model_dump(mode="json"), timeout=60.0
        )
        response.raise_for_status()
        return ComparisonReportResponse.model_validate(response.json())
