from typing import Optional

from aiohttp import ClientConnectorError

from monty.bot import Monty
from monty.constants import URLs
from monty.errors import APIError
from monty.log import get_logger


log = get_logger(__name__)

FAILED_REQUEST_ATTEMPTS = 3

PASTE_DISABLED = not URLs.paste_service


async def send_to_paste_service(bot: Monty, contents: str, *, extension: str = "") -> Optional[str]:
    """
    Upload `contents` to the paste service.

    `extension` is added to the output URL

    When an error occurs, `None` is returned, otherwise the generated URL with the suffix.
    """
    if PASTE_DISABLED:
        return "Sorry, paste isn't configured!"

    log.debug(f"Sending contents of size {len(contents.encode())} bytes to paste service.")
    paste_url = URLs.paste_service.format(key="api/new")
    json: dict[str, str] = {
        "content": contents,
    }
    if extension:
        json["language"] = extension
    response = None
    for attempt in range(1, FAILED_REQUEST_ATTEMPTS + 1):
        response_json = {}
        try:
            async with bot.http_session.post(paste_url, json=json) as response:
                response_json = await response.json()
                if not 200 <= response.status < 300 and attempt == FAILED_REQUEST_ATTEMPTS:
                    raise APIError("workbin", response.status, "The paste service could not be used at this time.")
        except ClientConnectorError:
            log.warning(
                f"Failed to connect to paste service at url {paste_url}, "
                f"trying again ({attempt}/{FAILED_REQUEST_ATTEMPTS})."
            )
            continue
        except Exception:
            log.exception(
                "An unexpected error has occurred during handling of the request, "
                f"trying again ({attempt}/{FAILED_REQUEST_ATTEMPTS})."
            )
            continue

        if "message" in response_json:
            log.warning(
                f"Paste service returned error {response_json['message']} with status code {response.status}, "
                f"trying again ({attempt}/{FAILED_REQUEST_ATTEMPTS})."
            )
            continue
        elif "key" in response_json:
            log.info(f"Successfully uploaded contents to paste service behind key {response_json['key']}.")

            paste_link = URLs.paste_service.format(key=f'?id={response_json["key"]}')
            if extension:
                paste_link += f"&language={extension}"

            return paste_link

        log.warning(
            f"Got unexpected JSON response from paste service: {response_json}\n"
            f"trying again ({attempt}/{FAILED_REQUEST_ATTEMPTS})."
        )

    raise APIError("workbin", response.status if response else 0, "The paste service could not be used at this time.")
