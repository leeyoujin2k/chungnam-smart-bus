import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import s from '../../app/App.module.css';
import { api, ApiError } from '../../shared/api/client';
import { MicrophoneIcon } from './MicrophoneIcon';
import { recordingType, recordSpeech, START_WAIT_MS } from './recordSpeech';

interface Recognition {
  lang: string;
  onspeechstart: (() => void) | null;
  onspeechend: (() => void) | null;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

const browser = window as Window & {
  SpeechRecognition?: new () => Recognition;
  webkitSpeechRecognition?: new () => Recognition;
};

const RETRY_DELAY_MS = 150;
const SPEAK_NEAR_MIC = '마이크 가까이에서 말씀해 주세요.';
const NO_SPEECH_MESSAGE = '들은 내용이 없어요.\n마이크 가까이에서 다시 말하거나 글로 입력해 주세요.';
const SAY_AGAIN = '서버 음성 인식에 연결하지 못했어요. 한 번 더 말씀해 주세요.';

type Phase = 'idle' | 'preparing' | 'waiting' | 'listening' | 'manual' | 'processing';

interface Props {
  disabled: boolean;
  onListen: () => void;
  onText: (text: string) => void;
  onListeningChange?: (listening: boolean) => void;
  children: ReactNode;
}

export function SpeechInput({ disabled, onListen, onText, onListeningChange, children }: Props) {
  const active = useRef<Recognition | null>(null);
  const capture = useRef<{ cancel: AbortController; finish: AbortController } | null>(null);
  const retry = useRef<number | null>(null);
  const nativeTimer = useRef<number | undefined>(undefined);
  const nativeStop = useRef<(() => void) | null>(null);
  const [phase, setPhase] = useState<Phase>('idle');
  const [message, setMessage] = useState('');
  const listening = phase !== 'idle';
  const SpeechRecognition = browser.SpeechRecognition ?? browser.webkitSpeechRecognition;
  const mimeType = recordingType();
  const available = Boolean(mimeType || SpeechRecognition);

  useEffect(() => { onListeningChange?.(listening); }, [listening, onListeningChange]);

  const abort = useCallback(() => {
    if (retry.current !== null) {
      window.clearTimeout(retry.current);
      retry.current = null;
    }
    window.clearTimeout(nativeTimer.current);
    nativeStop.current = null;
    const recognition = active.current;
    const recording = capture.current;
    active.current = null;
    capture.current = null;
    recognition?.abort();
    recording?.cancel.abort();
  }, []);

  const cancel = useCallback(() => {
    if (!active.current && !capture.current && retry.current === null) return;
    abort();
    setPhase('idle');
    setMessage('');
  }, [abort]);

  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden) cancel();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      abort();
    };
  }, [abort, cancel]);

  useEffect(() => {
    if (disabled) cancel();
  }, [disabled, cancel]);

  async function startCapture(type: string) {
    const session = { cancel: new AbortController(), finish: new AbortController() };
    const { signal } = session.cancel;
    capture.current = session;
    let browserFallback = false;
    onListen();
    setPhase('preparing');
    setMessage('마이크를 준비하고 있어요.');
    try {
      const audio = await recordSpeech(type, signal, session.finish.signal, phase => {
        setPhase(phase);
        setMessage(phase === 'waiting' ? SPEAK_NEAR_MIC
          : phase === 'manual' ? `${SPEAK_NEAR_MIC}\n말씀을 마치면 ‘말하기 끝내기’를 눌러 주세요.`
          : '듣고 있어요. 천천히 말씀해 주세요.');
      });
      if (signal.aborted) return;
      if (!audio?.size) {
        setMessage(NO_SPEECH_MESSAGE);
        return;
      }
      setPhase('processing');
      setMessage('인식 중이에요.');
      const { data } = await api.transcribe(audio, signal);
      if (signal.aborted) return;
      const transcript = data.text.trim().slice(0, 200);
      setMessage(transcript ? '' : NO_SPEECH_MESSAGE);
      if (transcript) onText(transcript);
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof ApiError) {
        browserFallback = Boolean(SpeechRecognition && (error.status === 404 || error.status >= 500
          || ['NETWORK_ERROR', 'TIMEOUT', 'INVALID_RESPONSE'].includes(error.code)));
        setMessage(error.message);
      } else if (error instanceof DOMException && ['NotAllowedError', 'SecurityError'].includes(error.name)) {
        setMessage('마이크 사용을 허용하거나 글로 입력해 주세요.');
      } else {
        setMessage('마이크 연결을 확인하거나 글로 입력해 주세요.');
      }
    } finally {
      if (capture.current === session) {
        capture.current = null;
        setPhase('idle');
      }
    }
    if (browserFallback) startNative(true);
  }

  function start() {
    if (capture.current || active.current || retry.current !== null) return;
    if (mimeType) void startCapture(mimeType);
    else startNative();
  }

  function startNative(fallback = false) {
    if (!SpeechRecognition) return;
    const Recognition = SpeechRecognition;
    const deadline = performance.now() + START_WAIT_MS;
    let heardSpeech = false;
    let stopping = false;
    let failureMessage = NO_SPEECH_MESSAGE;
    function finish(message?: string) {
      abort();
      setPhase('idle');
      setMessage(message ?? '');
    }

    function startAttempt() {
      try {
        const recognition = new Recognition();
        recognition.lang = 'ko-KR';
        active.current = recognition;
        recognition.onspeechstart = () => {
          if (active.current !== recognition || heardSpeech) return;
          heardSpeech = true;
          window.clearTimeout(nativeTimer.current);
          nativeTimer.current = window.setTimeout(() => finish(failureMessage), 12_000);
          setPhase('listening');
          setMessage('듣고 있어요. 천천히 말씀해 주세요.');
        };
        recognition.onspeechend = () => {
          if (active.current !== recognition) return;
          setPhase('processing');
          setMessage('인식 중이에요.');
        };
        recognition.onresult = event => {
          if (active.current !== recognition) return;
          const transcript = event.results[0]?.[0]?.transcript.trim().slice(0, 200);
          if (!transcript) return;
          finish();
          onText(transcript);
        };
        recognition.onerror = ({ error }) => {
          if (active.current !== recognition) return;
          // Wait for onend before reopening a recognizer that stopped early.
          if (error === 'no-speech') return;
          if (error === 'network') {
            failureMessage = '음성 인식 서비스에 연결하지 못했어요. 다시 시도하거나 글로 입력해 주세요.';
            return;
          }
          if (error === 'not-allowed' || error === 'service-not-allowed') {
            finish('마이크 사용을 허용하거나 글로 입력해 주세요.');
          } else if (error === 'audio-capture') {
            finish('마이크 연결을 확인하거나 글로 입력해 주세요.');
          } else {
            finish('음성을 인식하지 못했어요. 다시 말하거나 글로 입력해 주세요.');
          }
        };
        recognition.onend = () => {
          if (active.current !== recognition) return;
          active.current = null;
          const remaining = deadline - performance.now();
          if (heardSpeech || stopping || remaining <= 0) {
            finish(failureMessage);
            return;
          }
          retry.current = window.setTimeout(() => {
            retry.current = null;
            if (performance.now() < deadline) startAttempt();
            else finish(failureMessage);
          }, Math.min(RETRY_DELAY_MS, remaining));
        };
        recognition.start();
      } catch {
        finish('마이크를 시작하지 못했어요. 글로 입력해 주세요.');
      }
    }

    onListen();
    setPhase('waiting');
    setMessage(fallback ? SAY_AGAIN : SPEAK_NEAR_MIC);
    nativeStop.current = () => {
      stopping = true;
      try {
        if (active.current) active.current.stop();
        else finish(failureMessage);
      } catch {
        finish(failureMessage);
      }
    };
    nativeTimer.current = window.setTimeout(() => finish(failureMessage), START_WAIT_MS);
    startAttempt();
  }

  function finishRecording() {
    capture.current?.finish.abort();
    nativeStop.current?.();
  }

  const canFinish = phase === 'waiting' || phase === 'listening' || phase === 'manual';

  return <div className={s.destinationSpeech}>
    <div className={s.speechPrompt}>
      <p role="status">{available ? message : '이 브라우저에서는 글로 입력해 주세요.'}</p>
      {!listening && message !== NO_SPEECH_MESSAGE && children}
    </div>
    {available && (!listening
      ? <button type="button" className={`${s.primary} ${s.destinationMic}`} disabled={disabled} onClick={start}>
          <MicrophoneIcon className={s.micIcon} />말하기
        </button>
      : <div className={s.speechActions}>
          {canFinish && <button type="button" className={s.primary} onClick={finishRecording}>말하기 끝내기</button>}
          <button type="button" className={`${s.primary} ${s.secondary}`} onClick={cancel}>취소</button>
        </div>)}
  </div>;
}
