import abc
import asyncio

import aioxmpp.errors as errors
import aioxmpp.nonza as nonza
import aioxmpp.protocol as protocol
import aioxmpp.ssl_transport as ssl_transport

from aioxmpp.utils import namespaces


class BaseConnector(metaclass=abc.ABCMeta):
    @abc.abstractproperty
    def tls_supported(self):
        """
        Boolean which indicates whether TLS is supported by this connector.
        """

    @abc.abstractproperty
    def dane_supported(self):
        """
        Boolean which indicates whether DANE is supported by this connector.
        """

    @abc.abstractmethod
    @asyncio.coroutine
    def connect(self, loop, metadata, domain, host, port, negotiation_timeout):
        """
        Establish a :class:`.protocol.XMLStream` for `domain` with the given
        `host` at the given TCP `port`.

        `metadata` must be a :class:`.security_layer.SecurityLayer` instance to
        use for the connection. `loop` must be a :class:`asyncio.BaseEventLoop`
        to use.

        `negotiation_timeout` must be the maximum time in seconds to wait for
        the server to reply in each negotiation step.

        Return a triple consisting of the :class:`asyncio.Transport`, the
        :class:`.protocol.XMLStream` and the
        :class:`aioxmpp.nonza.StreamFeatures` of the stream.

        To detect the use of TLS on the stream, check whether
        :meth:`asyncio.Transport.get_extra_info` returns a non-:data:`None`
        value for ``"ssl_object"``.
        """


class STARTTLSConnector(BaseConnector):
    @property
    def tls_supported(self):
        return True

    @property
    def dane_supported(self):
        return False

    @asyncio.coroutine
    def connect(self, loop, metadata, domain, host, port,
                negotiation_timeout):
        features_future = asyncio.Future(loop=loop)

        stream = protocol.XMLStream(
            to=domain,
            features_future=features_future,
        )

        transport, _ = yield from ssl_transport.create_starttls_connection(
            loop,
            lambda: stream,
            host=host,
            port=port,
            peer_hostname=host,
            server_hostname=domain,
            use_starttls=True,
        )

        features = yield from features_future

        try:
            features[nonza.StartTLSFeature]
        except KeyError:
            if metadata.tls_required:
                message = (
                    "STARTTLS not supported by server, but required by client"
                )

                protocol.send_stream_error_and_close(
                    stream,
                    condition=(namespaces.streams, "policy-violation"),
                    text=message,
                )

                raise errors.TLSUnavailable(message)
            else:
                return transport, stream, (yield from features_future)

        response = yield from protocol.send_and_wait_for(
            stream,
            [
                nonza.StartTLS(),
            ],
            [
                nonza.StartTLSFailure,
                nonza.StartTLSProceed,
            ]
        )

        if not isinstance(response, nonza.StartTLSProceed):
            if metadata.tls_required:
                message = (
                    "server failed to STARTTLS"
                )

                protocol.send_stream_error_and_close(
                    stream,
                    condition=(namespaces.streams, "policy-violation"),
                    text=message,
                )

                raise errors.TLSUnavailable(message)
            return transport, stream, (yield from features_future)

        verifier = metadata.certificate_verifier_factory()
        yield from verifier.pre_handshake(transport)

        ssl_context = metadata.ssl_context_factory()
        verifier.setup_context(ssl_context)

        yield from stream.starttls(
            ssl_context=ssl_context,
            post_handshake_callback=verifier.post_handshake,
        )

        features_future = yield from protocol.reset_stream_and_get_features(
            stream,
            timeout=negotiation_timeout,
        )

        return transport, stream, features_future


class XMPPOverTLSConnector:
    """
    The XMPP-over-TLS connector implements the connection part of :xep:`368`.
    """
