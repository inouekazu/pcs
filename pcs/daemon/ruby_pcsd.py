import json
import logging
from base64 import b64decode, b64encode, binascii
from collections import namedtuple
from time import time as now

import pycurl
from tornado.gen import convert_yielded
from tornado.web import HTTPError
from tornado.httputil import split_host_and_port, HTTPServerRequest
from tornado.httpclient import AsyncHTTPClient
from tornado.curl_httpclient import CurlError


from pcs.daemon import log


SINATRA_GUI = "sinatra_gui"
SINATRA_REMOTE = "sinatra_remote"
SYNC_CONFIGS = "sync_configs"

DEFAULT_SYNC_CONFIG_DELAY = 5
RUBY_LOG_LEVEL_MAP = {
    "UNKNOWN": logging.NOTSET,
    "FATAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

class SinatraResult(namedtuple("SinatraResult", "headers, status, body")):
    @classmethod
    def from_response(cls, response):
        return cls(
            response["headers"],
            response["status"],
            response["body"]
        )

def log_group_id_generator():
    group_id = 0
    while True:
        group_id = group_id + 1 if group_id < 99999 else 0
        yield group_id

LOG_GROUP_ID = log_group_id_generator()

def process_response_logs(rb_log_list):
    if not rb_log_list:
        return

    group_id = next(LOG_GROUP_ID)
    for rb_log in rb_log_list:
        log.from_external_source(
            level=RUBY_LOG_LEVEL_MAP.get(rb_log["level"], logging.NOTSET),
            created=rb_log["timestamp_usec"] / 1000000,
            usecs=int(str(rb_log["timestamp_usec"])[-6:]),
            message=rb_log["message"],
            group_id=group_id
        )

class Wrapper:
    def __init__(self, pcsd_ruby_socket, debug=False):
        self.__debug = debug
        AsyncHTTPClient.configure('tornado.curl_httpclient.CurlAsyncHTTPClient')
        self.__client = AsyncHTTPClient()
        self.__pcsd_ruby_socket = pcsd_ruby_socket

    @staticmethod
    def get_sinatra_request(request: HTTPServerRequest):
        host, port = split_host_and_port(request.host)
        return {"env": {
            "PATH_INFO": request.path,
            "QUERY_STRING": request.query,
            "REMOTE_ADDR": request.remote_ip,
            "REMOTE_HOST": request.host,
            "REQUEST_METHOD": request.method,
            "REQUEST_URI": f"{request.protocol}://{request.host}{request.uri}",
            "SCRIPT_NAME": "",
            "SERVER_NAME": host,
            "SERVER_PORT": port,
            "SERVER_PROTOCOL": request.version,
            "HTTP_HOST": request.host,
            "HTTP_ACCEPT": "*/*",
            "HTTP_COOKIE": ";".join([
                v.OutputString() for v in request.cookies.values()
            ]),
            "HTTPS": "on" if request.protocol == "https" else "off",
            "HTTP_VERSION": request.version,
            "REQUEST_PATH": request.path,
            "rack.input": request.body.decode("utf8"),
        }}

    def prepare_curl_callback(self, curl):
        curl.setopt(pycurl.UNIX_SOCKET_PATH, self.__pcsd_ruby_socket)
        curl.setopt(pycurl.TIMEOUT, 70)

    async def run_ruby(self, request_type, request=None):
        """
        request_type: SINATRA_GUI|SINATRA_REMOTE|SYNC_CONFIGS
        request: result of get_sinatra_request|None
            i.e. it has structure returned by get_sinatra_request if the request
            is not None - so we can get SERVER_NAME and  SERVER_PORT
        """
        request = request or {}
        request.update({"type": request_type})
        request_json = json.dumps(request)

        if self.__debug:
            log.pcsd.debug("Ruby daemon request: '%s'", request_json)
        # We do not need location for cummunication with ruby itself since we
        # communicate via unix socket. But it is required by AsyncHTTPClient so
        # "localhost" is used.
        tornado_request = b64encode(request_json.encode()).decode()
        try:
            ruby_response = await self.__client.fetch(
                "localhost",
                method="POST",
                body=f"TORNADO_REQUEST={tornado_request}",
                prepare_curl_callback=self.prepare_curl_callback,
            )
        except CurlError as e:
            log.pcsd.error(
                "Cannot connect to ruby daemon (message: '%s'). Is it running?",
                e
            )
            raise HTTPError(500)

        try:
            response = json.loads(ruby_response.body)
            logs = response.pop("logs", [])
            if "body" in response:
                body = b64decode(response.pop("body"))
                if self.__debug:
                    log.pcsd.debug(
                        "Ruby daemon response (without logs and body): '%s'",
                        json.dumps(response)
                    )
                    log.pcsd.debug("Ruby daemon response body: '%s'", body)
                response["body"] = body

            elif self.__debug:
                log.pcsd.debug(
                    "Ruby daemon response (without logs): '%s'",
                    json.dumps(response)
                )
            process_response_logs(logs)
            return response
        except (json.JSONDecodeError, binascii.Error) as e:
            if self.__debug:
                log.pcsd.debug("Ruby daemon response: '%s'", ruby_response.body)
            log.pcsd.error("Cannot decode json from ruby pcsd wrapper: '%s'", e)
            raise HTTPError(500)

    async def request_gui(
        self, request: HTTPServerRequest, user, groups, is_authenticated
    ) -> SinatraResult:
        sinatra_request = self.get_sinatra_request(request)
        # Sessions handling was removed from ruby. However, some session
        # information is needed for ruby code (e.g. rendering some parts of
        # templates). So this information must be sent to ruby by another way.
        sinatra_request.update({
            "session": {
                "username": user,
                "groups": groups,
                "is_authenticated": is_authenticated,
            }
        })
        response = await convert_yielded(self.run_ruby(
            SINATRA_GUI,
            sinatra_request
        ))
        return SinatraResult.from_response(response)

    async def request_remote(self, request: HTTPServerRequest) -> SinatraResult:
        response = await convert_yielded(self.run_ruby(
            SINATRA_REMOTE,
            self.get_sinatra_request(request)
        ))
        return SinatraResult.from_response(response)

    async def sync_configs(self):
        try:
            response = await convert_yielded(self.run_ruby(SYNC_CONFIGS))
            return response["next"]
        except HTTPError:
            log.pcsd.error("Config synchronization failed")
            return int(now()) + DEFAULT_SYNC_CONFIG_DELAY
