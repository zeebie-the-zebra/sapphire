// settings-tabs/conversation.js - True speech (conversation) mode tuning
export default {
    id: 'conversation',
    name: 'Conversation',
    icon: '💬',  // 💬
    description: 'True speech mode — continuous-listen tuning to cut false triggers',
    essentialKeys: [
        'CONVERSATION_DTLN',
        'CONVERSATION_VAD_THRESHOLD',
        'CONVERSATION_BARGE_HOLD_MS',
        'CONVERSATION_MIN_SPEECH_MS',
        'CONVERSATION_ENDPOINT_SILENCE_MS',
    ],

    render(ctx) {
        return ctx.renderFields(this.essentialKeys);
    }
};
