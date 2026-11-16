import type { MicVAD } from '@ricky0123/vad-web';

const AUDIO_TYPES = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'];
const MAX_RECORDING_MS = 14_000; // Server accepts up to 15 seconds, including encoder padding.
export const START_WAIT_MS = 7_500;
const END_SILENCE_MS = 1_800;
const DETECTOR_LOAD_MS = 8_000;
const DETECTOR_START_MS = 2_000;
const MIN_SPEECH_MS = 128;
const RUMBLE_CUTOFF_HZ = 100;
const VAD_ASSETS = `${import.meta.env.BASE_URL}speech-vad/`;
const VAD_WORKLET = 'vad.worklet.bundle.min.js';

export type RecordingPhase = 'waiting' | 'listening' | 'manual';

export function recordingType() {
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') return undefined;
  return AUDIO_TYPES.find(type => MediaRecorder.isTypeSupported(type));
}

function openMicrophone() {
  const media = navigator.mediaDevices;
  const supported = media.getSupportedConstraints?.() ?? {};
  const audio: MediaTrackConstraints = {};
  if (supported.echoCancellation) audio.echoCancellation = { ideal: true };
  if (supported.noiseSuppression) audio.noiseSuppression = { ideal: true };
  if (supported.autoGainControl) audio.autoGainControl = { ideal: true };
  if (supported.channelCount) audio.channelCount = { ideal: 1 };
  return media.getUserMedia({ audio: Object.keys(audio).length ? audio : true });
}

function filterMicrophone(microphone: MediaStream, context?: AudioContext) {
  if (context?.state !== 'running') return microphone;
  try {
    const source = context.createMediaStreamSource(microphone);
    const filter = context.createBiquadFilter();
    filter.type = 'highpass';
    filter.frequency.value = RUMBLE_CUTOFF_HZ;
    filter.Q.value = -3; // Butterworth response. Web Audio specifies high-pass Q in dB.
    const destination = context.createMediaStreamDestination();
    destination.channelCount = 1;
    source.connect(filter);
    filter.connect(destination);
    return destination.stream;
  } catch {
    // Keep recording with the device stream on browsers without this audio graph.
    return microphone;
  }
}

function waitForDetector(task: Promise<MicVAD>, signal: AbortSignal): Promise<MicVAD | undefined> {
  return new Promise(resolve => {
    const finish = (detector?: MicVAD) => {
      window.clearTimeout(timer);
      signal.removeEventListener('abort', cancelled);
      resolve(detector);
    };
    const cancelled = () => finish();
    const timer = window.setTimeout(cancelled, DETECTOR_LOAD_MS);
    signal.addEventListener('abort', cancelled, { once: true });
    if (signal.aborted) cancelled();
    void task.then(detector => finish(detector), cancelled);
  });
}

export async function recordSpeech(
  mimeType: string, signal: AbortSignal, finish: AbortSignal, onPhase: (phase: RecordingPhase) => void,
): Promise<Blob | null> {
  let microphone: MediaStream | undefined;
  let stream: MediaStream | undefined;
  let context: AudioContext | undefined;
  let recorder: MediaRecorder | undefined;
  let loading: Promise<MicVAD> | undefined;
  let starting: Promise<void> | undefined;
  let maxTimer: number | undefined;
  let waitTimer: number | undefined;
  let startTimer: number | undefined;
  let active = true;
  let automatic = false;
  let ready = false;
  let heardSpeech = false;
  let discard = false;

  const stop = () => {
    if (!active) return;
    active = false;
    window.clearTimeout(maxTimer);
    window.clearTimeout(waitTimer);
    window.clearTimeout(startTimer);
    if (recorder && recorder.state !== 'inactive') recorder.stop();
  };
  const noSpeech = () => {
    if (!active) return;
    discard = true;
    stop();
  };
  const closeContext = () => {
    if (context && context.state !== 'closed') void context.close().catch(() => {});
  };
  const manual = () => {
    if (!active || !automatic) return;
    automatic = false;
    window.clearTimeout(startTimer);
    window.clearTimeout(waitTimer);
    // The recorder may still be using the audio filter in this context.
    onPhase('manual');
  };

  async function prepareDetector(audioContext: AudioContext) {
    const { MicVAD } = await import('@ricky0123/vad-web');
    const assets = new URL(VAD_ASSETS, document.baseURI).href;
    // Load the worklet and model before opening the microphone. A short first word can arrive as
    // soon as permission is granted, while cold model loading can take seconds.
    await audioContext.audioWorklet.addModule(assets + VAD_WORKLET);
    signal.throwIfAborted();
    return MicVAD.new({
      model: 'v5',
      processorType: 'AudioWorklet',
      startOnLoad: false,
      baseAssetPath: assets,
      onnxWASMBasePath: assets,
      audioContext,
      getStream: async () => {
        if (!stream) throw new Error('Microphone is not open');
        return stream;
      },
      // This function owns the microphone tracks, which are shared with MediaRecorder.
      pauseStream: async () => {},
      ortConfig: ort => { ort.env.wasm.numThreads = 1; },
      positiveSpeechThreshold: 0.55,
      negativeSpeechThreshold: 0.35,
      redemptionMs: END_SILENCE_MS,
      minSpeechMs: MIN_SPEECH_MS,
      onFrameProcessed: () => {
        if (!active || !automatic || ready) return;
        ready = true;
        window.clearTimeout(startTimer);
        waitTimer = window.setTimeout(noSpeech, START_WAIT_MS);
        onPhase('waiting');
      },
      onSpeechRealStart: () => {
        if (!active || !automatic || heardSpeech) return;
        heardSpeech = true;
        window.clearTimeout(waitTimer);
        onPhase('listening');
      },
      onSpeechEnd: () => {
        if (active && automatic && heardSpeech) stop();
      },
    });
  }

  try {
    let detector: MicVAD | undefined;
    if (typeof AudioContext !== 'undefined') {
      try {
        context = new AudioContext();
        // Resume inside the button gesture where required by browser autoplay rules.
        void context.resume().catch(() => {});
        loading = prepareDetector(context);
        detector = await waitForDetector(loading, signal);
      } catch {
        // Recording still works on browsers that cannot start the audio detector.
      }
    }
    signal.throwIfAborted();
    microphone = await openMicrophone();
    signal.throwIfAborted();
    // VAD and the server recording hear the same input. A high-pass filter reduces
    // engine rumble and wind without muting quiet speech based on volume.
    stream = filterMicrophone(microphone, context);
    const capture = new MediaRecorder(stream, { mimeType });
    recorder = capture;
    const chunks: Blob[] = [];
    signal.addEventListener('abort', stop, { once: true });
    finish.addEventListener('abort', stop, { once: true });

    return await new Promise<Blob | null>((resolve, reject) => {
      // Retain everything from mic start, including audio before VAD confirms speech.
      capture.ondataavailable = ({ data }) => { if (data.size) chunks.push(data); };
      capture.onstop = () => resolve(discard ? null : new Blob(chunks, { type: capture.mimeType || mimeType }));
      capture.onerror = () => reject(new Error('Recording failed'));
      capture.start();
      if (finish.aborted) { stop(); return; }
      maxTimer = window.setTimeout(() => {
        if (ready && automatic && !heardSpeech) noSpeech();
        else stop();
      }, MAX_RECORDING_MS);
      if (detector) {
        automatic = true;
        startTimer = window.setTimeout(manual, DETECTOR_START_MS);
        starting = detector.start().catch(manual);
      } else {
        onPhase('manual');
      }
    });
  } finally {
    stop();
    signal.removeEventListener('abort', stop);
    finish.removeEventListener('abort', stop);
    if (stream !== microphone) stream?.getTracks().forEach(track => track.stop());
    microphone?.getTracks().forEach(track => track.stop());
    closeContext();
    // Ignore late callbacks after cancellation and release the detector when its setup settles.
    void loading?.then(async detector => { await starting; await detector.destroy(); }).catch(() => {});
  }
}
