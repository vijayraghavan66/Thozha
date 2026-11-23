package com.jarvis.companion.voice;

import kotlin.jvm.functions.Function1;

/** Concrete JNI-visible callback; sherpa-onnx 2.x reflects invoke(float[]) directly. */
public final class KokoroPcmChunkCallback implements Function1<float[], Integer> {
    public interface Handler { int onChunk(float[] samples); }
    private final Handler handler;
    public KokoroPcmChunkCallback(Handler handler) { this.handler = handler; }
    @Override public Integer invoke(float[] samples) { return handler.onChunk(samples); }
}
