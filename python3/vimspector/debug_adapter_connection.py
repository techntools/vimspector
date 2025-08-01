# vimspector - A multi-language debugging system for Vim
# Copyright 2018 Ben Jackson
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import json
import vim

from vimspector import utils

DEFAULT_SYNC_TIMEOUT = 5000
DEFAULT_ASYNC_TIMEOUT = 15000


class PendingRequest( object ):
  def __init__( self, msg, handler, failure_handler, expiry_id ):
    self.msg = msg
    self.handler = handler
    self.failure_handler = failure_handler
    self.expiry_id = expiry_id


class DebugAdapterConnection( object ):
  def __init__( self,
                handlers,
                session_id,
                send_func,
                sync_timeout = None,
                async_timeout = None ):
    self._logger = logging.getLogger( __name__ + '.' + str( session_id ) )
    utils.SetUpLogging( self._logger, session_id )

    if not sync_timeout:
      sync_timeout = DEFAULT_SYNC_TIMEOUT
    if not async_timeout:
      async_timeout = DEFAULT_ASYNC_TIMEOUT

    self._Write = send_func
    self._SetState( 'READ_HEADER' )
    self._buffer = bytes()
    self._handlers = handlers
    self._session_id = session_id
    self._next_message_id = 1
    self._outstanding_requests = {}
    self.async_timeout = async_timeout
    self.sync_timeout = sync_timeout

  def GetSessionId( self ):
    return self._session_id

  def DoRequest( self,
                 handler,
                 msg,
                 failure_handler=None,
                 timeout = None ):

    if timeout is None:
      timeout = self.async_timeout

    this_id = self._next_message_id
    self._next_message_id += 1

    msg[ 'seq' ] = this_id
    msg[ 'type' ] = 'request'

    expiry_id = vim.eval(
      'timer_start( {}, '
      '             function( "vimspector#internal#channel#Timeout", '
      '                       [ {} ] ) )'.format(
        timeout,
        self._session_id ) )

    request = PendingRequest( msg,
                              handler,
                              failure_handler,
                              expiry_id )
    self._outstanding_requests[ this_id ] = request

    if not self._SendMessage( msg ):
      self._AbortRequest( request, 'Unable to send message' )


  def DoRequestSync( self, msg, timeout = None ):
    result = {}

    if timeout is None:
      timeout = self.sync_timeout

    def handler( msg ):
      result[ 'response' ] = msg

    def failure_handler( reason, msg ):
      result[ 'response' ] = msg
      result[ 'exception' ] = RuntimeError( reason )

    self.DoRequest( handler, msg, failure_handler, timeout )

    to_wait = timeout + 1000
    while not result and to_wait >= 0:
      vim.command( 'sleep 10m' )
      to_wait -= 10

    if result.get( 'exception' ) is not None:
      raise result[ 'exception' ]

    if result.get( 'response' ) is None:
      raise RuntimeError( "No response" )

    return result[ 'response' ]


  def OnRequestTimeout( self, timer_id ):
    request_id = None
    for seq, request in self._outstanding_requests.items():
      if request.expiry_id == timer_id:
        request_id = seq
        break

    # Avoid modifying _outstanding_requests while looping
    if request_id is not None:
      request = self._outstanding_requests.pop( request_id )
      self._AbortRequest( request, 'Timeout' )

  def DoResponse( self, request, error, response ):
    this_id = self._next_message_id
    self._next_message_id += 1

    msg = {}
    msg[ 'seq' ] = this_id
    msg[ 'type' ] = 'response'
    msg[ 'request_seq' ] = request[ 'seq' ]
    msg[ 'command' ] = request[ 'command' ]
    msg[ 'body' ] = response
    if error:
      msg[ 'success' ] = False
      msg[ 'message' ] = error
    else:
      msg[ 'success' ] = True

    self._SendMessage( msg )

  def Reset( self ):
    self._Write = None
    self._handlers = None

    while self._outstanding_requests:
      _, request = self._outstanding_requests.popitem()
      self._AbortRequest( request, 'Closing down' )

  def _AbortRequest( self, request, reason ):
    self._logger.debug( '{}: Aborting request {}'.format( reason,
                                                          request.msg ) )
    _KillTimer( request )
    if request.failure_handler:
      request.failure_handler( reason, {} )
    else:
      utils.UserMessage( 'Request for {} aborted: {}'.format(
        request.msg[ 'command' ],
        reason ) )


  def OnData( self, data ):
    data = bytes( data, 'utf-8' )
    # self._logger.debug( 'Received ({0}/{1}): {2},'.format( type( data ),
    #                                                   len( data ),
    #                                                   data ) )

    self._buffer += data

    while True:
      if self._state == 'READ_HEADER':
        self._ReadHeaders()

      if self._state == 'READ_BODY':
        self._ReadBody()
      else:
        break

      if self._state != 'READ_HEADER':
        # We ran out of data whilst reading the body. Await more data.
        break

  def _SetState( self, state ):
    self._state = state
    if state == 'READ_HEADER':
      self._headers = {}

  def _SendMessage( self, msg ):
    if not self._Write:
      # Connection was destroyed
      return False

    msg = json.dumps( msg )
    self._logger.debug( 'Sending Message: {0}'.format( msg ) )

    data = 'Content-Length: {0}\r\n\r\n{1}'.format( len( msg ), msg )
    # self._logger.debug( 'Sending: {0}'.format( data ) )
    return self._Write( data )

  def _ReadHeaders( self ):
    parts = self._buffer.split( bytes( '\r\n\r\n', 'utf-8' ), 1 )

    if len( parts ) > 1:
      headers = parts[ 0 ]
      for header_line in headers.split( bytes( '\r\n', 'utf-8' ) ):
        if bytes( '\n', 'utf-8' ) in header_line:
          # Work around bugs in cppdbg where mono spams nonesense to stdout.
          # This is such a dodgyhack, but it fixes the issues.
          header_line = header_line.split( bytes( '\n', 'utf-8' ) )[ -1 ]

        if header_line.strip():
          key, value = str( header_line, 'utf-8' ).split( ':', 1 )
          self._headers[ key ] = value

      # Chomp (+4 for the 2 newlines which were the separator)
      # self._buffer = self._buffer[ len( headers[ 0 ] ) + 4 : ]
      self._buffer = parts[ 1 ]
      self._SetState( 'READ_BODY' )
      return

    # otherwise waiting for more data

  def _ReadBody( self ):
    try:
      content_length = int( self._headers[ 'Content-Length' ] )
    except KeyError:
      # Ug oh. We seem to have all the headers, but no Content-Length
      # Skip to reading headers. Because, what else can we do.
      self._logger.error( 'Missing Content-Length header in: {0}'.format(
        json.dumps( self._headers ) ) )

      self._buffer = bytes( '', 'utf-8' )
      self._SetState( 'READ_HEADER' )
      return

    if len( self._buffer ) < content_length:
      # Need more data
      assert self._state == 'READ_BODY'
      return

    payload = str( self._buffer[ : content_length ], 'utf-8' )
    self._buffer = self._buffer[ content_length : ]

    # self._logger.debug( 'Message received (raw): %s', payload )
    # We read the message, so the next time we get data from the socket it must
    # be a header.
    self._SetState( 'READ_HEADER' )

    try:
      message = json.loads( payload, strict = False )
    except Exception:
      self._logger.exception( "Invalid message received: %s", payload )
      raise

    self._logger.debug( 'Message received: {0}'.format( message ) )

    self._OnMessageReceived( message )


  def _OnMessageReceived( self, message ):
    if not self._handlers:
      return

    if message[ 'type' ] == 'response':
      try:
        request = self._outstanding_requests.pop( message[ 'request_seq' ] )
      except KeyError:
        # Sigh. It looks like the ms python debug adapter sends duplicate
        # initialize responses.
        utils.UserMessage(
          "Protocol error: duplicate response for request {}".format(
            message[ 'request_seq' ] ) )
        self._logger.exception( 'Duplicate response: {}'.format( message ) )
        return

      _KillTimer( request )

      if message[ 'success' ]:
        if request.handler:
          request.handler( message )
      else:
        reason = message.get( 'message' )
        error = message.get( 'body', {} ).get( 'error', {} )
        if error:
          try:
            fmt = error[ 'format' ]
            variables = error.get( 'variables', {} )
            reason = fmt.format( **variables )
          except Exception:
            self._logger.exception( "Failed to parse error, using default: %s",
                                    error )

        if request.failure_handler:
          self._logger.info( 'Request failed (handled): %s', reason )
          request.failure_handler( reason, message )
        else:
          self._logger.error( 'Request failed (unhandled): %s', reason )
          for h in self._handlers:
            if 'OnFailure' in dir( h ):
              if h.OnFailure( reason, request.msg, message ):
                break

    elif message[ 'type' ] == 'event':
      method = 'OnEvent_' + message[ 'event' ]
      for h in self._handlers:
        if method in dir( h ):
          if getattr( h, method )( message ):
            break
    elif message[ 'type' ] == 'request':
      method = 'OnRequest_' + message[ 'command' ]
      for h in self._handlers:
        if method in dir( h ):
          if getattr( h, method )( message ):
            break


def _KillTimer( request ):
  if request.expiry_id is not None:
    vim.eval( 'timer_stop( {} )'.format( request.expiry_id ) )
    request.expiry_id = None
