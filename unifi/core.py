import asyncio
import inspect
import ssl

import backoff
import websockets


class RetryableError(Exception):
    pass


class Core(object):
    def __init__(self, args, camera, logger):
        self.host = args.host
        self.token = args.token
        self.mac = args.mac
        self.logger = logger
        self.cam = camera

        # Set up ssl context for requests
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
        self.ssl_context.load_cert_chain(args.cert, args.cert)

    async def run(self) -> None:
        uri = "wss://{}:7442/camera/1.0/ws?token={}".format(self.host, self.token)
        headers = {"camera-mac": self.mac}
        has_connected = False

        @backoff.on_predicate(
            backoff.expo,
            lambda retryable: retryable,
            factor=2,
            jitter=None,
            max_value=10,
            logger=self.logger,
        )
        async def connect():
            nonlocal has_connected
            self.logger.info(f"Creating ws connection to {uri}")
            try:
                connect_kwargs = {
                    "ssl": self.ssl_context,
                    "subprotocols": ["secure_transfer"],
                }
                try:
                    connect_sig = inspect.signature(websockets.connect)
                except (TypeError, ValueError):
                    connect_sig = None

                if connect_sig is not None and "additional_headers" in connect_sig.parameters:
                    connect_kwargs["additional_headers"] = headers
                else:
                    connect_kwargs["extra_headers"] = headers

                try:
                    ws = await websockets.connect(uri, **connect_kwargs)
                except TypeError as e:
                    msg = str(e)
                    if (
                        "unexpected keyword argument 'extra_headers'" in msg
                        and "extra_headers" in connect_kwargs
                    ):
                        connect_kwargs["additional_headers"] = connect_kwargs.pop(
                            "extra_headers"
                        )
                        ws = await websockets.connect(uri, **connect_kwargs)
                    elif (
                        "unexpected keyword argument 'additional_headers'" in msg
                        and "additional_headers" in connect_kwargs
                    ):
                        connect_kwargs["extra_headers"] = connect_kwargs.pop(
                            "additional_headers"
                        )
                        ws = await websockets.connect(uri, **connect_kwargs)
                    else:
                        raise
                has_connected = True
            except websockets.exceptions.InvalidStatusCode as e:
                if e.status_code == 403:
                    self.logger.error(
                        f"The token '{self.token}'"
                        " is invalid. Please generate a new one and try again."
                    )
                # Hitting rate-limiting
                elif e.status_code == 429:
                    return True
                raise
            except asyncio.exceptions.TimeoutError:
                self.logger.info(f"Connection to {self.host} timed out.")
                return True
            except ConnectionRefusedError:
                self.logger.info(f"Connection to {self.host} refused.")
                return True

            tasks = [
                asyncio.create_task(self.cam._run(ws)),
                asyncio.create_task(self.cam.run()),
            ]
            try:
                await asyncio.gather(*tasks)
            except RetryableError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                return True
            finally:
                await self.cam.close()

        await connect()
