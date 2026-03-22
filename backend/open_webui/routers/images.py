import asyncio
import base64
import io
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from open_webui.config import CACHE_DIR
from open_webui.constants import ERROR_MESSAGES
from open_webui.env import ENABLE_FORWARD_USER_INFO_HEADERS, SRC_LOG_LEVELS
from open_webui.routers.files import upload_file
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import has_permission
from open_webui.utils.user_connections import get_user_connections
from open_webui.utils.images.comfyui import (
    ComfyUIGenerateImageForm,
    ComfyUIWorkflow,
    comfyui_generate_image,
)
from pydantic import BaseModel

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["IMAGES"])

IMAGE_CACHE_DIR = CACHE_DIR / "image" / "generations"
IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


router = APIRouter()

COMFYUI_WORKFLOW_NODE_MAPPING_INVALID = (
    "ComfyUI workflow node mapping is invalid. Please update the workflow node IDs to match the current workflow."
)

def _can_use_image_generation(request: Request, user) -> bool:
    """Server-side permission gate for image generation (matches builtin_tools behavior)."""
    if getattr(user, "role", None) == "admin":
        return True
    try:
        return has_permission(
            user.id, "features.image_generation", request.app.state.config.USER_PERMISSIONS
        )
    except Exception:
        return False


def _normalize_engine(value: Optional[str]) -> str:
    engine = (value or "").strip().lower()
    # Historically, "" has been treated as Automatic1111 in this codebase.
    return engine or "automatic1111"


def _is_non_empty(value: Optional[str]) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _parse_comfyui_workflow_config(request: Request) -> dict:
    workflow = json.loads(request.app.state.config.COMFYUI_WORKFLOW or "{}")
    if not isinstance(workflow, dict):
        raise HTTPException(
            status_code=400, detail=COMFYUI_WORKFLOW_NODE_MAPPING_INVALID
        )
    return workflow


def _collect_missing_comfyui_node_refs(
    workflow: dict, workflow_nodes: list[dict], *, node_type: Optional[str] = None
) -> list[tuple[str, str]]:
    workflow_node_ids = {str(node_id) for node_id in workflow.keys()}
    missing_refs: list[tuple[str, str]] = []

    for node in workflow_nodes or []:
        current_type = str(node.get("type") or "")
        if node_type is not None and current_type != node_type:
            continue

        for raw_node_id in node.get("node_ids") or []:
            node_id = str(raw_node_id).strip()
            if node_id and node_id not in workflow_node_ids:
                missing_refs.append((current_type or "custom", node_id))

    return missing_refs


def _validate_comfyui_workflow_node_mapping(
    workflow: dict, workflow_nodes: list[dict], *, node_type: Optional[str] = None
) -> None:
    missing_refs = _collect_missing_comfyui_node_refs(
        workflow, workflow_nodes, node_type=node_type
    )
    if missing_refs:
        refs = ", ".join(f"{kind}({node_id})" for kind, node_id in missing_refs)
        log.warning(f"Invalid ComfyUI workflow node mapping detected: {refs}")
        raise HTTPException(
            status_code=400, detail=COMFYUI_WORKFLOW_NODE_MAPPING_INVALID
        )


def _get_user_provider_urls_keys(user, provider: str) -> tuple[list[str], list[str]]:
    conns = get_user_connections(user)
    cfg = conns.get(provider) if isinstance(conns, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}

    if provider == "openai":
        urls_key = "OPENAI_API_BASE_URLS"
        keys_key = "OPENAI_API_KEYS"
    elif provider == "gemini":
        urls_key = "GEMINI_API_BASE_URLS"
        keys_key = "GEMINI_API_KEYS"
    else:
        return [], []

    base_urls = list(cfg.get(urls_key) or [])
    keys = list(cfg.get(keys_key) or [])

    # Keep list lengths aligned (do not mutate persisted settings here).
    if len(keys) != len(base_urls):
        if len(keys) > len(base_urls):
            keys = keys[: len(base_urls)]
        else:
            keys = keys + [""] * (len(base_urls) - len(keys))

    return base_urls, keys


def _pick_personal_connection(
    base_urls: list[str], keys: list[str], preferred_index: Optional[int] = None
) -> Optional[tuple[int, str, str]]:
    usable: list[tuple[int, str, str]] = []
    for idx, (url, key) in enumerate(zip(base_urls or [], keys or [])):
        u = str(url or "").strip()
        k = str(key or "").strip()
        if u and k:
            usable.append((idx, u, k))

    if not usable:
        return None

    if preferred_index is not None:
        for item in usable:
            if item[0] == preferred_index:
                return item

    return usable[0]


def _get_personal_connection_exact(
    base_urls: list[str], keys: list[str], index: Optional[int]
) -> Optional[tuple[int, str, str]]:
    if index is None:
        return None
    if index < 0:
        return None
    if index >= len(base_urls):
        return None
    u = str((base_urls[index] if index < len(base_urls) else "") or "").strip()
    k = str((keys[index] if index < len(keys) else "") or "").strip()
    if not u or not k:
        return None
    return index, u, k


def _shared_key_available(request: Request, engine: str) -> bool:
    cfg = request.app.state.config
    engine = _normalize_engine(engine)

    if engine == "openai":
        return _is_non_empty(getattr(cfg, "IMAGES_OPENAI_API_BASE_URL", "")) and _is_non_empty(
            getattr(cfg, "IMAGES_OPENAI_API_KEY", "")
        )
    if engine == "gemini":
        return _is_non_empty(getattr(cfg, "IMAGES_GEMINI_API_BASE_URL", "")) and _is_non_empty(
            getattr(cfg, "IMAGES_GEMINI_API_KEY", "")
        )
    if engine == "comfyui":
        return _is_non_empty(getattr(cfg, "COMFYUI_BASE_URL", ""))
    if engine in ("automatic1111", ""):
        return _is_non_empty(getattr(cfg, "AUTOMATIC1111_BASE_URL", ""))

    return False


@router.get("/config")
async def get_config(request: Request, user=Depends(get_admin_user)):
    return {
        "enabled": request.app.state.config.ENABLE_IMAGE_GENERATION,
        "engine": request.app.state.config.IMAGE_GENERATION_ENGINE,
        "prompt_generation": request.app.state.config.ENABLE_IMAGE_PROMPT_GENERATION,
        "shared_key_enabled": getattr(
            request.app.state.config, "ENABLE_IMAGE_GENERATION_SHARED_KEY", False
        ),
        "openai": {
            "OPENAI_API_BASE_URL": request.app.state.config.IMAGES_OPENAI_API_BASE_URL,
            "OPENAI_API_KEY": request.app.state.config.IMAGES_OPENAI_API_KEY,
        },
        "automatic1111": {
            "AUTOMATIC1111_BASE_URL": request.app.state.config.AUTOMATIC1111_BASE_URL,
            "AUTOMATIC1111_API_AUTH": request.app.state.config.AUTOMATIC1111_API_AUTH,
            "AUTOMATIC1111_CFG_SCALE": request.app.state.config.AUTOMATIC1111_CFG_SCALE,
            "AUTOMATIC1111_SAMPLER": request.app.state.config.AUTOMATIC1111_SAMPLER,
            "AUTOMATIC1111_SCHEDULER": request.app.state.config.AUTOMATIC1111_SCHEDULER,
        },
        "comfyui": {
            "COMFYUI_BASE_URL": request.app.state.config.COMFYUI_BASE_URL,
            "COMFYUI_API_KEY": request.app.state.config.COMFYUI_API_KEY,
            "COMFYUI_WORKFLOW": request.app.state.config.COMFYUI_WORKFLOW,
            "COMFYUI_WORKFLOW_NODES": request.app.state.config.COMFYUI_WORKFLOW_NODES,
        },
        "gemini": {
            "GEMINI_API_BASE_URL": request.app.state.config.IMAGES_GEMINI_API_BASE_URL,
            "GEMINI_API_KEY": request.app.state.config.IMAGES_GEMINI_API_KEY,
        },
    }


class OpenAIConfigForm(BaseModel):
    OPENAI_API_BASE_URL: str
    OPENAI_API_KEY: str


class Automatic1111ConfigForm(BaseModel):
    AUTOMATIC1111_BASE_URL: str
    AUTOMATIC1111_API_AUTH: str
    AUTOMATIC1111_CFG_SCALE: Optional[str | float | int]
    AUTOMATIC1111_SAMPLER: Optional[str]
    AUTOMATIC1111_SCHEDULER: Optional[str]


class ComfyUIConfigForm(BaseModel):
    COMFYUI_BASE_URL: str
    COMFYUI_API_KEY: str
    COMFYUI_WORKFLOW: str
    COMFYUI_WORKFLOW_NODES: list[dict]


class GeminiConfigForm(BaseModel):
    GEMINI_API_BASE_URL: str
    GEMINI_API_KEY: str


class ConfigForm(BaseModel):
    enabled: bool
    engine: str
    prompt_generation: bool
    shared_key_enabled: bool = False
    openai: OpenAIConfigForm
    automatic1111: Automatic1111ConfigForm
    comfyui: ComfyUIConfigForm
    gemini: GeminiConfigForm


@router.post("/config/update")
async def update_config(
    request: Request, form_data: ConfigForm, user=Depends(get_admin_user)
):
    request.app.state.config.IMAGE_GENERATION_ENGINE = form_data.engine
    request.app.state.config.ENABLE_IMAGE_GENERATION = form_data.enabled
    request.app.state.config.ENABLE_IMAGE_GENERATION_SHARED_KEY = (
        form_data.shared_key_enabled
    )

    request.app.state.config.ENABLE_IMAGE_PROMPT_GENERATION = (
        form_data.prompt_generation
    )

    request.app.state.config.IMAGES_OPENAI_API_BASE_URL = (
        form_data.openai.OPENAI_API_BASE_URL
    )
    request.app.state.config.IMAGES_OPENAI_API_KEY = form_data.openai.OPENAI_API_KEY

    request.app.state.config.IMAGES_GEMINI_API_BASE_URL = (
        form_data.gemini.GEMINI_API_BASE_URL
    )
    request.app.state.config.IMAGES_GEMINI_API_KEY = form_data.gemini.GEMINI_API_KEY

    request.app.state.config.AUTOMATIC1111_BASE_URL = (
        form_data.automatic1111.AUTOMATIC1111_BASE_URL
    )
    request.app.state.config.AUTOMATIC1111_API_AUTH = (
        form_data.automatic1111.AUTOMATIC1111_API_AUTH
    )

    request.app.state.config.AUTOMATIC1111_CFG_SCALE = (
        float(form_data.automatic1111.AUTOMATIC1111_CFG_SCALE)
        if form_data.automatic1111.AUTOMATIC1111_CFG_SCALE
        else None
    )
    request.app.state.config.AUTOMATIC1111_SAMPLER = (
        form_data.automatic1111.AUTOMATIC1111_SAMPLER
        if form_data.automatic1111.AUTOMATIC1111_SAMPLER
        else None
    )
    request.app.state.config.AUTOMATIC1111_SCHEDULER = (
        form_data.automatic1111.AUTOMATIC1111_SCHEDULER
        if form_data.automatic1111.AUTOMATIC1111_SCHEDULER
        else None
    )

    request.app.state.config.COMFYUI_BASE_URL = (
        form_data.comfyui.COMFYUI_BASE_URL.strip("/")
    )
    request.app.state.config.COMFYUI_API_KEY = form_data.comfyui.COMFYUI_API_KEY

    request.app.state.config.COMFYUI_WORKFLOW = form_data.comfyui.COMFYUI_WORKFLOW
    request.app.state.config.COMFYUI_WORKFLOW_NODES = (
        form_data.comfyui.COMFYUI_WORKFLOW_NODES
    )

    return {
        "enabled": request.app.state.config.ENABLE_IMAGE_GENERATION,
        "engine": request.app.state.config.IMAGE_GENERATION_ENGINE,
        "prompt_generation": request.app.state.config.ENABLE_IMAGE_PROMPT_GENERATION,
        "shared_key_enabled": request.app.state.config.ENABLE_IMAGE_GENERATION_SHARED_KEY,
        "openai": {
            "OPENAI_API_BASE_URL": request.app.state.config.IMAGES_OPENAI_API_BASE_URL,
            "OPENAI_API_KEY": request.app.state.config.IMAGES_OPENAI_API_KEY,
        },
        "automatic1111": {
            "AUTOMATIC1111_BASE_URL": request.app.state.config.AUTOMATIC1111_BASE_URL,
            "AUTOMATIC1111_API_AUTH": request.app.state.config.AUTOMATIC1111_API_AUTH,
            "AUTOMATIC1111_CFG_SCALE": request.app.state.config.AUTOMATIC1111_CFG_SCALE,
            "AUTOMATIC1111_SAMPLER": request.app.state.config.AUTOMATIC1111_SAMPLER,
            "AUTOMATIC1111_SCHEDULER": request.app.state.config.AUTOMATIC1111_SCHEDULER,
        },
        "comfyui": {
            "COMFYUI_BASE_URL": request.app.state.config.COMFYUI_BASE_URL,
            "COMFYUI_API_KEY": request.app.state.config.COMFYUI_API_KEY,
            "COMFYUI_WORKFLOW": request.app.state.config.COMFYUI_WORKFLOW,
            "COMFYUI_WORKFLOW_NODES": request.app.state.config.COMFYUI_WORKFLOW_NODES,
        },
        "gemini": {
            "GEMINI_API_BASE_URL": request.app.state.config.IMAGES_GEMINI_API_BASE_URL,
            "GEMINI_API_KEY": request.app.state.config.IMAGES_GEMINI_API_KEY,
        },
    }


def get_automatic1111_api_auth(request: Request):
    if request.app.state.config.AUTOMATIC1111_API_AUTH is None:
        return ""
    else:
        auth1111_byte_string = request.app.state.config.AUTOMATIC1111_API_AUTH.encode(
            "utf-8"
        )
        auth1111_base64_encoded_bytes = base64.b64encode(auth1111_byte_string)
        auth1111_base64_encoded_string = auth1111_base64_encoded_bytes.decode("utf-8")
        return f"Basic {auth1111_base64_encoded_string}"


@router.get("/config/url/verify")
async def verify_url(request: Request, user=Depends(get_admin_user)):
    if request.app.state.config.IMAGE_GENERATION_ENGINE == "automatic1111":
        try:
            r = requests.get(
                url=f"{request.app.state.config.AUTOMATIC1111_BASE_URL}/sdapi/v1/options",
                headers={"authorization": get_automatic1111_api_auth(request)},
            )
            r.raise_for_status()
            return True
        except Exception:
            raise HTTPException(status_code=400, detail=ERROR_MESSAGES.INVALID_URL)
    elif request.app.state.config.IMAGE_GENERATION_ENGINE == "comfyui":

        headers = None
        if request.app.state.config.COMFYUI_API_KEY:
            headers = {
                "Authorization": f"Bearer {request.app.state.config.COMFYUI_API_KEY}"
            }

        try:
            r = requests.get(
                url=f"{request.app.state.config.COMFYUI_BASE_URL}/object_info",
                headers=headers,
            )
            r.raise_for_status()
            return True
        except Exception:
            raise HTTPException(status_code=400, detail=ERROR_MESSAGES.INVALID_URL)
    else:
        return True


@router.get("/usage/config")
async def get_usage_config(request: Request, user=Depends(get_verified_user)):
    """
    Safe, non-admin config for the image generation UI (no keys).

    Used by the image generation page to:
    - decide whether the feature is enabled,
    - show engine + defaults,
    - determine if shared-key fallback is allowed/available.
    """
    if not _can_use_image_generation(request, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    cfg = request.app.state.config
    engine = _normalize_engine(getattr(cfg, "IMAGE_GENERATION_ENGINE", ""))
    shared_enabled = bool(getattr(cfg, "ENABLE_IMAGE_GENERATION_SHARED_KEY", False))
    shared_available = _shared_key_available(request, engine) if shared_enabled else False

    personal_supported = engine in ("openai", "gemini")
    provider = engine if personal_supported else None

    return {
        "enabled": bool(getattr(cfg, "ENABLE_IMAGE_GENERATION", False)),
        "engine": engine,
        "defaults": {
            "model": getattr(cfg, "IMAGE_GENERATION_MODEL", "") or "",
            "size": getattr(cfg, "IMAGE_SIZE", "") or "",
            "steps": getattr(cfg, "IMAGE_STEPS", 0),
        },
        "shared_key": {"enabled": shared_enabled, "available": shared_available},
        "personal_key": {"supported": personal_supported, "provider": provider},
    }


def set_image_model(request: Request, model: str):
    log.info(f"Setting image model to {model}")
    request.app.state.config.IMAGE_GENERATION_MODEL = model
    if request.app.state.config.IMAGE_GENERATION_ENGINE in ["", "automatic1111"]:
        api_auth = get_automatic1111_api_auth(request)
        r = requests.get(
            url=f"{request.app.state.config.AUTOMATIC1111_BASE_URL}/sdapi/v1/options",
            headers={"authorization": api_auth},
        )
        options = r.json()
        if model != options["sd_model_checkpoint"]:
            options["sd_model_checkpoint"] = model
            r = requests.post(
                url=f"{request.app.state.config.AUTOMATIC1111_BASE_URL}/sdapi/v1/options",
                json=options,
                headers={"authorization": api_auth},
            )
    return request.app.state.config.IMAGE_GENERATION_MODEL


def get_image_model(request):
    if request.app.state.config.IMAGE_GENERATION_ENGINE == "openai":
        return (
            request.app.state.config.IMAGE_GENERATION_MODEL
            if request.app.state.config.IMAGE_GENERATION_MODEL
            else "dall-e-2"
        )
    elif request.app.state.config.IMAGE_GENERATION_ENGINE == "gemini":
        return (
            request.app.state.config.IMAGE_GENERATION_MODEL
            if request.app.state.config.IMAGE_GENERATION_MODEL
            else "imagen-3.0-generate-002"
        )
    elif request.app.state.config.IMAGE_GENERATION_ENGINE == "comfyui":
        return (
            request.app.state.config.IMAGE_GENERATION_MODEL
            if request.app.state.config.IMAGE_GENERATION_MODEL
            else ""
        )
    elif (
        request.app.state.config.IMAGE_GENERATION_ENGINE == "automatic1111"
        or request.app.state.config.IMAGE_GENERATION_ENGINE == ""
    ):
        try:
            r = requests.get(
                url=f"{request.app.state.config.AUTOMATIC1111_BASE_URL}/sdapi/v1/options",
                headers={"authorization": get_automatic1111_api_auth(request)},
            )
            options = r.json()
            return options["sd_model_checkpoint"]
        except Exception as e:
            raise HTTPException(status_code=400, detail=ERROR_MESSAGES.DEFAULT(e))


class ImageConfigForm(BaseModel):
    MODEL: str
    IMAGE_SIZE: str
    IMAGE_STEPS: int
    IMAGE_MODEL_FILTER_REGEX: Optional[str] = None


@router.get("/image/config")
async def get_image_config(request: Request, user=Depends(get_admin_user)):
    return {
        "MODEL": request.app.state.config.IMAGE_GENERATION_MODEL,
        "IMAGE_SIZE": request.app.state.config.IMAGE_SIZE,
        "IMAGE_STEPS": request.app.state.config.IMAGE_STEPS,
        "IMAGE_MODEL_FILTER_REGEX": request.app.state.config.IMAGE_MODEL_FILTER_REGEX,
    }


@router.post("/image/config/update")
async def update_image_config(
    request: Request, form_data: ImageConfigForm, user=Depends(get_admin_user)
):
    set_image_model(request, form_data.MODEL)

    pattern = r"^\d+x\d+$"
    if re.match(pattern, form_data.IMAGE_SIZE):
        request.app.state.config.IMAGE_SIZE = form_data.IMAGE_SIZE
    else:
        raise HTTPException(
            status_code=400,
            detail=ERROR_MESSAGES.INCORRECT_FORMAT("  (e.g., 512x512)."),
        )

    if form_data.IMAGE_STEPS >= 0:
        request.app.state.config.IMAGE_STEPS = form_data.IMAGE_STEPS
    else:
        raise HTTPException(
            status_code=400,
            detail=ERROR_MESSAGES.INCORRECT_FORMAT("  (e.g., 50)."),
        )

    if form_data.IMAGE_MODEL_FILTER_REGEX is not None:
        # Validate regex syntax
        if form_data.IMAGE_MODEL_FILTER_REGEX:
            try:
                re.compile(form_data.IMAGE_MODEL_FILTER_REGEX)
            except re.error:
                raise HTTPException(
                    status_code=400,
                    detail=ERROR_MESSAGES.INCORRECT_FORMAT("  (invalid regex pattern)."),
                )
        request.app.state.config.IMAGE_MODEL_FILTER_REGEX = form_data.IMAGE_MODEL_FILTER_REGEX

    return {
        "MODEL": request.app.state.config.IMAGE_GENERATION_MODEL,
        "IMAGE_SIZE": request.app.state.config.IMAGE_SIZE,
        "IMAGE_STEPS": request.app.state.config.IMAGE_STEPS,
        "IMAGE_MODEL_FILTER_REGEX": request.app.state.config.IMAGE_MODEL_FILTER_REGEX,
    }


@router.get("/models")
def get_models(request: Request, user=Depends(get_verified_user)):
    if not _can_use_image_generation(request, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    models = []
    try:
        if request.app.state.config.IMAGE_GENERATION_ENGINE == "openai":
            models = [
                {"id": "dall-e-2", "name": "DALL·E 2"},
                {"id": "dall-e-3", "name": "DALL·E 3"},
                {"id": "gpt-image-1", "name": "GPT Image 1"},
            ]
        elif request.app.state.config.IMAGE_GENERATION_ENGINE == "gemini":
            models = [
                {"id": "imagen-3-0-generate-002", "name": "imagen-3.0 generate-002"},
            ]
        elif request.app.state.config.IMAGE_GENERATION_ENGINE == "comfyui":
            headers = {
                "Authorization": f"Bearer {request.app.state.config.COMFYUI_API_KEY}"
            }
            r = requests.get(
                url=f"{request.app.state.config.COMFYUI_BASE_URL}/object_info",
                headers=headers,
            )
            info = r.json()

            workflow = _parse_comfyui_workflow_config(request)
            _validate_comfyui_workflow_node_mapping(
                workflow,
                request.app.state.config.COMFYUI_WORKFLOW_NODES,
                node_type="model",
            )
            model_node_id = None

            for node in request.app.state.config.COMFYUI_WORKFLOW_NODES:
                if node["type"] == "model":
                    if node["node_ids"]:
                        model_node_id = node["node_ids"][0]
                    break

            if model_node_id:
                model_list_key = None

                log.info(workflow[model_node_id]["class_type"])
                for key in info[workflow[model_node_id]["class_type"]]["input"][
                    "required"
                ]:
                    if "_name" in key:
                        model_list_key = key
                        break

                if model_list_key:
                    models = list(
                        map(
                            lambda model: {"id": model, "name": model},
                            info[workflow[model_node_id]["class_type"]]["input"][
                                "required"
                            ][model_list_key][0],
                        )
                    )
            else:
                models = list(
                    map(
                        lambda model: {"id": model, "name": model},
                        info["CheckpointLoaderSimple"]["input"]["required"][
                            "ckpt_name"
                        ][0],
                    )
                )
        elif (
            request.app.state.config.IMAGE_GENERATION_ENGINE == "automatic1111"
            or request.app.state.config.IMAGE_GENERATION_ENGINE == ""
        ):
            r = requests.get(
                url=f"{request.app.state.config.AUTOMATIC1111_BASE_URL}/sdapi/v1/sd-models",
                headers={"authorization": get_automatic1111_api_auth(request)},
            )
            raw_models = r.json()
            models = list(
                map(
                    lambda model: {"id": model["title"], "name": model["model_name"]},
                    raw_models,
                )
            )
    except Exception as e:
        raise HTTPException(status_code=400, detail=ERROR_MESSAGES.DEFAULT(e))

    # Apply model regex filter if configured
    regex = request.app.state.config.IMAGE_MODEL_FILTER_REGEX
    if regex and models:
        try:
            pattern = re.compile(regex)
            models = [m for m in models if pattern.search(m.get("id", "") or "")]
        except re.error:
            log.warning(f"Invalid IMAGE_MODEL_FILTER_REGEX: {regex}")

    return models


class GenerateImageForm(BaseModel):
    model: Optional[str] = None
    prompt: str
    size: Optional[str] = None
    n: int = 1
    negative_prompt: Optional[str] = None
    credential_source: Optional[str] = None
    connection_index: Optional[int] = None
    steps: Optional[int] = None
    background: Optional[str] = None


def load_b64_image_data(b64_str):
    try:
        if "," in b64_str:
            header, encoded = b64_str.split(",", 1)
            mime_type = header.split(";")[0]
            img_data = base64.b64decode(encoded)
        else:
            mime_type = "image/png"
            img_data = base64.b64decode(b64_str)
        return img_data, mime_type
    except Exception as e:
        log.exception(f"Error loading image data: {e}")
        return None


def load_url_image_data(url, headers=None):
    try:
        # Basic SSRF protection: reject private/internal URLs
        from urllib.parse import urlparse
        import ipaddress

        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or hostname.endswith(".local"):
            log.warning(f"Blocked SSRF attempt to internal URL: {hostname}")
            return None
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                log.warning(f"Blocked SSRF attempt to private IP: {ip}")
                return None
        except ValueError:
            pass  # hostname is not an IP literal, OK

        if headers:
            r = requests.get(url, headers=headers, timeout=15)
        else:
            r = requests.get(url, timeout=15)

        r.raise_for_status()
        if r.headers["content-type"].split("/")[0] == "image":
            mime_type = r.headers["content-type"]
            return r.content, mime_type
        else:
            log.error("Url does not point to an image.")
            return None

    except Exception as e:
        log.exception(f"Error saving image: {e}")
        return None


def upload_image(request, image_metadata, image_data, content_type, user):
    image_format = mimetypes.guess_extension(content_type)
    file = UploadFile(
        file=io.BytesIO(image_data),
        filename=f"generated-image{image_format}",  # will be converted to a unique ID on upload_file
        headers={
            "content-type": content_type,
        },
    )
    file_item = upload_file(request, file, user, file_metadata=image_metadata)
    url = request.app.url_path_for("get_file_content_by_id", id=file_item.id)
    return url


@router.post("/generations")
async def image_generations(
    request: Request,
    form_data: GenerateImageForm,
    user=Depends(get_verified_user),
):
    if not _can_use_image_generation(request, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    if not request.app.state.config.ENABLE_IMAGE_GENERATION:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Image generation is disabled by the administrator.",
        )

    effective_size = request.app.state.config.IMAGE_SIZE
    if form_data.size is not None:
        if re.match(r"^\d+x\d+$", form_data.size):
            effective_size = form_data.size
        else:
            raise HTTPException(
                status_code=400,
                detail=ERROR_MESSAGES.INCORRECT_FORMAT("  (e.g., 512x512)."),
            )

    width, height = tuple(map(int, effective_size.split("x")))
    selected_model = form_data.model or request.app.state.config.IMAGE_GENERATION_MODEL

    r = None
    try:
        if request.app.state.config.IMAGE_GENERATION_ENGINE == "openai":
            cfg = request.app.state.config

            credential_source = (form_data.credential_source or "auto").strip().lower()
            if credential_source not in ("auto", "personal", "shared"):
                credential_source = "auto"

            shared_enabled = bool(getattr(cfg, "ENABLE_IMAGE_GENERATION_SHARED_KEY", False))
            shared_available = _shared_key_available(request, "openai")

            personal_urls, personal_keys = _get_user_provider_urls_keys(user, "openai")

            effective_source: str = "auto"
            chosen_idx: Optional[int] = None

            if credential_source == "shared":
                if not shared_enabled:
                    raise HTTPException(
                        status_code=400,
                        detail="Workspace shared key is disabled by the administrator.",
                    )
                if not shared_available:
                    raise HTTPException(
                        status_code=400,
                        detail="Workspace shared key is not configured. Please contact your administrator.",
                    )

                effective_source = "shared"
                base_url = cfg.IMAGES_OPENAI_API_BASE_URL
                api_key = cfg.IMAGES_OPENAI_API_KEY
            elif credential_source == "personal":
                if form_data.connection_index is not None:
                    chosen = _get_personal_connection_exact(
                        personal_urls, personal_keys, form_data.connection_index
                    )
                    if chosen is None:
                        raise HTTPException(
                            status_code=400,
                            detail="Selected personal connection is not configured. Go to Settings > Connections.",
                        )
                else:
                    chosen = _pick_personal_connection(personal_urls, personal_keys)
                    if chosen is None:
                        raise HTTPException(
                            status_code=400,
                            detail="No personal connection found. Go to Settings > Connections to add your key.",
                        )

                chosen_idx, base_url, api_key = chosen
                effective_source = "personal"
            else:  # auto
                chosen = _get_personal_connection_exact(
                    personal_urls, personal_keys, form_data.connection_index
                ) or _pick_personal_connection(personal_urls, personal_keys)

                if chosen is not None:
                    chosen_idx, base_url, api_key = chosen
                    effective_source = "personal"
                elif shared_enabled and shared_available:
                    effective_source = "shared"
                    base_url = cfg.IMAGES_OPENAI_API_BASE_URL
                    api_key = cfg.IMAGES_OPENAI_API_KEY
                else:
                    if shared_enabled and not shared_available:
                        raise HTTPException(
                            status_code=400,
                            detail="Shared key is enabled but not configured. Contact your administrator or set your own key in Settings > Connections.",
                        )
                    raise HTTPException(
                        status_code=400,
                        detail="No image generation connection configured. Go to Settings > Connections to add your key.",
                    )

            base_url = str(base_url or "").strip().rstrip("/")
            api_key = str(api_key or "").strip()

            headers = {}
            headers["Content-Type"] = "application/json"

            # Azure OpenAI auto-detection: use api-key header instead of Bearer
            is_azure = "openai.azure.com" in base_url
            if is_azure:
                headers["api-key"] = api_key
            else:
                headers["Authorization"] = f"Bearer {api_key}"

            try:
                log.info(
                    f"image_generation user_id={user.id} engine=openai credential_source={effective_source} connection_index={chosen_idx if chosen_idx is not None else ''} model={selected_model or ''} size={effective_size} n={form_data.n} steps={(form_data.steps if form_data.steps is not None else '')}"
                )
            except Exception:
                pass

            if ENABLE_FORWARD_USER_INFO_HEADERS:
                headers["X-OpenWebUI-User-Name"] = user.name
                headers["X-OpenWebUI-User-Id"] = user.id
                headers["X-OpenWebUI-User-Email"] = user.email
                headers["X-OpenWebUI-User-Role"] = user.role

            data = {
                "model": selected_model if selected_model != "" else "dall-e-2",
                "prompt": form_data.prompt,
                "n": form_data.n,
                "size": effective_size,
                "response_format": "b64_json",
            }

            # gpt-image-1 supports background parameter (transparent/opaque)
            if form_data.background:
                data["background"] = form_data.background

            # Build generation URL (Azure uses different URL pattern with api-version)
            if is_azure:
                gen_url = f"{base_url}/images/generations?api-version=2024-02-01"
            else:
                gen_url = f"{base_url}/images/generations"

            # Use asyncio.to_thread for the requests.post call
            r = await asyncio.to_thread(
                requests.post,
                url=gen_url,
                json=data,
                headers=headers,
            )

            r.raise_for_status()
            res = r.json()

            images = []

            for image in res["data"]:
                if image_url := image.get("url", None):
                    image_data, content_type = load_url_image_data(image_url, headers)
                else:
                    image_data, content_type = load_b64_image_data(image["b64_json"])

                url = upload_image(request, data, image_data, content_type, user)
                images.append({"url": url})
            return images

        elif request.app.state.config.IMAGE_GENERATION_ENGINE == "gemini":
            cfg = request.app.state.config

            credential_source = (form_data.credential_source or "auto").strip().lower()
            if credential_source not in ("auto", "personal", "shared"):
                credential_source = "auto"

            shared_enabled = bool(getattr(cfg, "ENABLE_IMAGE_GENERATION_SHARED_KEY", False))
            shared_available = _shared_key_available(request, "gemini")

            personal_urls, personal_keys = _get_user_provider_urls_keys(user, "gemini")

            effective_source: str = "auto"
            chosen_idx: Optional[int] = None

            if credential_source == "shared":
                if not shared_enabled:
                    raise HTTPException(
                        status_code=400,
                        detail="Workspace shared key is disabled by the administrator.",
                    )
                if not shared_available:
                    raise HTTPException(
                        status_code=400,
                        detail="Workspace shared key is not configured. Please contact your administrator.",
                    )

                effective_source = "shared"
                base_url = cfg.IMAGES_GEMINI_API_BASE_URL
                api_key = cfg.IMAGES_GEMINI_API_KEY
            elif credential_source == "personal":
                if form_data.connection_index is not None:
                    chosen = _get_personal_connection_exact(
                        personal_urls, personal_keys, form_data.connection_index
                    )
                    if chosen is None:
                        raise HTTPException(
                            status_code=400,
                            detail="Selected personal connection is not configured. Go to Settings > Connections.",
                        )
                else:
                    chosen = _pick_personal_connection(personal_urls, personal_keys)
                    if chosen is None:
                        raise HTTPException(
                            status_code=400,
                            detail="No personal connection found. Go to Settings > Connections to add your key.",
                        )

                chosen_idx, base_url, api_key = chosen
                effective_source = "personal"
            else:  # auto
                chosen = _get_personal_connection_exact(
                    personal_urls, personal_keys, form_data.connection_index
                ) or _pick_personal_connection(personal_urls, personal_keys)

                if chosen is not None:
                    chosen_idx, base_url, api_key = chosen
                    effective_source = "personal"
                elif shared_enabled and shared_available:
                    effective_source = "shared"
                    base_url = cfg.IMAGES_GEMINI_API_BASE_URL
                    api_key = cfg.IMAGES_GEMINI_API_KEY
                else:
                    if shared_enabled and not shared_available:
                        raise HTTPException(
                            status_code=400,
                            detail="Shared key is enabled but not configured. Contact your administrator or set your own key in Settings > Connections.",
                        )
                    raise HTTPException(
                        status_code=400,
                        detail="No image generation connection configured. Go to Settings > Connections to add your key.",
                    )

            base_url = str(base_url or "").strip().rstrip("/")
            api_key = str(api_key or "").strip()

            headers = {}
            headers["Content-Type"] = "application/json"
            headers["x-goog-api-key"] = api_key

            model = selected_model if selected_model else get_image_model(request)
            data = {
                "instances": {"prompt": form_data.prompt},
                "parameters": {
                    "sampleCount": form_data.n,
                    "outputOptions": {"mimeType": "image/png"},
                },
            }

            # Use asyncio.to_thread for the requests.post call
            r = await asyncio.to_thread(
                requests.post,
                url=f"{base_url}/models/{model}:predict",
                json=data,
                headers=headers,
            )

            try:
                log.info(
                    f"image_generation user_id={user.id} engine=gemini credential_source={effective_source} connection_index={chosen_idx if chosen_idx is not None else ''} model={model or ''} size={effective_size} n={form_data.n} steps={(form_data.steps if form_data.steps is not None else '')}"
                )
            except Exception:
                pass

            r.raise_for_status()
            res = r.json()

            images = []
            for image in res["predictions"]:
                image_data, content_type = load_b64_image_data(
                    image["bytesBase64Encoded"]
                )
                url = upload_image(request, data, image_data, content_type, user)
                images.append({"url": url})

            return images

        elif request.app.state.config.IMAGE_GENERATION_ENGINE == "comfyui":
            workflow = _parse_comfyui_workflow_config(request)
            _validate_comfyui_workflow_node_mapping(
                workflow, request.app.state.config.COMFYUI_WORKFLOW_NODES
            )
            data = {
                "prompt": form_data.prompt,
                "width": width,
                "height": height,
                "n": form_data.n,
            }

            if request.app.state.config.IMAGE_STEPS is not None:
                data["steps"] = request.app.state.config.IMAGE_STEPS

            # Per-request steps override
            if form_data.steps is not None:
                data["steps"] = form_data.steps

            if form_data.negative_prompt is not None:
                data["negative_prompt"] = form_data.negative_prompt

            form_data = ComfyUIGenerateImageForm(
                **{
                    "workflow": ComfyUIWorkflow(
                        **{
                            "workflow": json.dumps(workflow),
                            "nodes": request.app.state.config.COMFYUI_WORKFLOW_NODES,
                        }
                    ),
                    **data,
                }
            )
            res = await comfyui_generate_image(
                selected_model,
                form_data,
                user.id,
                request.app.state.config.COMFYUI_BASE_URL,
                request.app.state.config.COMFYUI_API_KEY,
            )
            log.debug(f"res: {res}")

            images = []

            for image in res["data"]:
                headers = None
                if request.app.state.config.COMFYUI_API_KEY:
                    headers = {
                        "Authorization": f"Bearer {request.app.state.config.COMFYUI_API_KEY}"
                    }

                image_data, content_type = load_url_image_data(image["url"], headers)
                url = upload_image(
                    request,
                    form_data.model_dump(exclude_none=True),
                    image_data,
                    content_type,
                    user,
                )
                images.append({"url": url})
            return images
        elif (
            request.app.state.config.IMAGE_GENERATION_ENGINE == "automatic1111"
            or request.app.state.config.IMAGE_GENERATION_ENGINE == ""
        ):
            if form_data.model:
                set_image_model(request, form_data.model)

            data = {
                "prompt": form_data.prompt,
                "batch_size": form_data.n,
                "width": width,
                "height": height,
            }

            if request.app.state.config.IMAGE_STEPS is not None:
                data["steps"] = request.app.state.config.IMAGE_STEPS

            # Per-request steps override
            if form_data.steps is not None:
                data["steps"] = form_data.steps

            if form_data.negative_prompt is not None:
                data["negative_prompt"] = form_data.negative_prompt

            if request.app.state.config.AUTOMATIC1111_CFG_SCALE:
                data["cfg_scale"] = request.app.state.config.AUTOMATIC1111_CFG_SCALE

            if request.app.state.config.AUTOMATIC1111_SAMPLER:
                data["sampler_name"] = request.app.state.config.AUTOMATIC1111_SAMPLER

            if request.app.state.config.AUTOMATIC1111_SCHEDULER:
                data["scheduler"] = request.app.state.config.AUTOMATIC1111_SCHEDULER

            # Use asyncio.to_thread for the requests.post call
            r = await asyncio.to_thread(
                requests.post,
                url=f"{request.app.state.config.AUTOMATIC1111_BASE_URL}/sdapi/v1/txt2img",
                json=data,
                headers={"authorization": get_automatic1111_api_auth(request)},
            )

            res = r.json()
            log.debug(f"res: {res}")

            images = []

            for image in res["images"]:
                image_data, content_type = load_b64_image_data(image)
                url = upload_image(
                    request,
                    {**data, "info": res["info"]},
                    image_data,
                    content_type,
                    user,
                )
                images.append({"url": url})
            return images
    except Exception as e:
        error = e
        if r != None:
            data = r.json()
            if "error" in data:
                error = data["error"]["message"]
        raise HTTPException(status_code=400, detail=ERROR_MESSAGES.DEFAULT(error))
