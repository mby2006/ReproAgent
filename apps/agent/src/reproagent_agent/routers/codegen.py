from fastapi import APIRouter
from reproagent_schemas import (
    ClassifyRequest,
    CodegenRequest,
    CodegenResponse,
    ErrorClassification,
    RepairRequest,
    RepairResponse,
    StaticValidateRequest,
    ValidationReport,
)

from ..codegen import classify_error, generate_code, repair_bundle, validate_bundle

router = APIRouter(prefix="/v1/codegen", tags=["codegen"])


@router.post("", response_model=CodegenResponse)
async def generate(body: CodegenRequest) -> CodegenResponse:
    return CodegenResponse(bundle=generate_code(body.card, body.plan))


@router.post("/validate", response_model=ValidationReport)
async def validate(body: StaticValidateRequest) -> ValidationReport:
    return validate_bundle(body.bundle)


@router.post("/classify", response_model=ErrorClassification)
async def classify(body: ClassifyRequest) -> ErrorClassification:
    return classify_error(body)


@router.post("/repair", response_model=RepairResponse)
async def repair(body: RepairRequest) -> RepairResponse:
    return repair_bundle(body)