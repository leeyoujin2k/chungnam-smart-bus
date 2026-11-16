from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
import wave

from fastapi.testclient import TestClient
import pytest

from backend.api.errors import ServiceError
from backend.config import Settings
from backend.main import create_app
from backend.services.speech import SAMPLE_RATE, SpeechTranscriber


@pytest.fixture
def speech_app():
    return create_app(Settings(database_url='sqlite:///:memory:'), transport_catalog='cheonan')


def test_forwards_browser_recording_to_transcriber(speech_app, monkeypatch):
    def recognize(recording, audio_format):
        assert recording == b'webm recording'
        assert audio_format == 'matroska'
        return '중앙시장 가고 싶어요'

    monkeypatch.setattr(speech_app.state.speech, 'transcribe', recognize)
    with TestClient(speech_app) as client:
        result = client.post('/api/v1/speech/transcriptions', content=b'webm recording',
                             headers={'Content-Type': 'audio/webm;codecs=opus'})
    assert result.status_code == 200
    assert result.json()['data'] == {'text': '중앙시장 가고 싶어요'}


@pytest.mark.parametrize('content_type,recording,status,code', [
    ('text/plain', b'recording', 415, 'UNSUPPORTED_AUDIO'),
    ('audio/webm', b'', 400, 'INVALID_AUDIO'),
    ('audio/webm', b'x' * (2 * 1024 * 1024 + 1), 413, 'SPEECH_TOO_LONG'),
], ids=['unsupported-format', 'empty', 'oversize'])
def test_rejects_invalid_recordings(speech_app, content_type, recording, status, code):
    with TestClient(speech_app) as client:
        result = client.post('/api/v1/speech/transcriptions', content=recording,
                             headers={'Content-Type': content_type})
    assert result.status_code == status
    assert result.json()['error']['code'] == code


def wave_recording(seconds):
    audio = BytesIO()
    with wave.open(audio, 'wb') as writer:
        writer.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', ''))
        writer.writeframes(bytes(2 * SAMPLE_RATE * seconds))
    return audio.getvalue()


def test_speech_limit_allows_a_slow_phrase_and_rejects_oversize_audio():
    pytest.importorskip('av')
    pytest.importorskip('numpy')
    transcriber = SpeechTranscriber()
    transcriber.model = Mock()
    transcriber.model.transcribe.return_value = ([], None)
    assert transcriber.transcribe(wave_recording(15), 'wav') == ''
    assert len(transcriber.model.transcribe.call_args.args[0]) == 15 * SAMPLE_RATE
    with pytest.raises(ServiceError) as failure:
        transcriber.transcribe(wave_recording(16), 'wav')
    assert failure.value.code == 'SPEECH_TOO_LONG'


def test_speech_drops_a_repetitive_segment_and_keeps_the_recognized_sentence():
    pytest.importorskip('av')
    pytest.importorskip('numpy')
    transcriber = SpeechTranscriber()
    transcriber.model = Mock()
    transcriber.model.transcribe.return_value = (iter([
        SimpleNamespace(text='천안경과 ' * 40, compression_ratio=4.5),
        SimpleNamespace(text='중앙시장 가고 싶어요', compression_ratio=1.2),
    ]), None)
    assert transcriber.transcribe(wave_recording(3), 'wav') == '중앙시장 가고 싶어요'
