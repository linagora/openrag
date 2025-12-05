#!/usr/bin/env python3

from flask import Flask, request, jsonify
import json
import time
import uuid
from typing import Dict, Any, List
import threading

app = Flask(__name__)

HOST = "0.0.0.0"
PORT = 8080

# Constant response for chat completions
CONSTANT_RESPONSE = "This is a mock AI response. The user said: "

# Model information
AVAILABLE_MODELS = {
    "mock-model": {
        "id": "mock-model",
        "object": "model",
        "created": 1687882411,
        "owned_by": "mock-company"
    }
}

def create_chat_completion_response(
    messages: List[Dict[str, str]],
    model: str = "mock-model"
) -> Dict[str, Any]:
    """Create a mock chat completion response."""

    # Extract user messages for the constant response
    user_messages = [
        msg["content"] for msg in messages
        if msg["role"] == "user"
    ]
    last_user_message = user_messages[-1] if user_messages else ""

    # Generate response content
    response_content = CONSTANT_RESPONSE + last_user_message

    # Create response ID
    response_id = f"chatcmpl-{uuid.uuid4().hex}"

    # Current timestamp
    created = int(time.time())

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_content,
                },
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": len(response_content.split()),
            "total_tokens": 10 + len(response_content.split()),
        }
    }

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    """Handle chat completion requests."""
    try:
        data = request.get_json()

        # Extract parameters with defaults
        messages = data.get('messages', [])
        model = data.get('model', 'mock-model')
        stream = data.get('stream', False)

        # Validate required fields
        if not messages:
            return jsonify({
                "error": {
                    "message": "Missing required field: messages",
                    "type": "invalid_request_error",
                    "code": "missing_messages"
                }
            }), 400

        # Validate model
        if model not in AVAILABLE_MODELS:
            return jsonify({
                "error": {
                    "message": f"The model '{model}' does not exist",
                    "type": "invalid_request_error",
                    "code": "model_not_found"
                }
            }), 404

        # Handle streaming response
        if stream:
            def generate():
                response_content = CONSTANT_RESPONSE
                if messages:
                    user_messages = [
                        msg["content"] for msg in messages
                        if msg["role"] == "user"
                    ]
                    if user_messages:
                        response_content += user_messages[-1]

                # Split response into chunks for streaming
                words = response_content.split()
                for i, word in enumerate(words):
                    chunk = {
                        "id": f"chatcmpl-{uuid.uuid4().hex()}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "content": word + (" " if i < len(words) - 1 else "")
                                },
                                "finish_reason": None if i < len(words) - 1 else "stop",
                            }
                        ]
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return app.response_class(generate(), mimetype='text/event-stream')

        # Regular non-streaming response
        response = create_chat_completion_response(messages, model)
        return jsonify(response)

    except Exception as e:
        return jsonify({
            "error": {
                "message": str(e),
                "type": "server_error",
                "code": "internal_error"
            }
        }), 500

@app.route('/v1/models', methods=['GET'])
def list_models():
    """List available models."""
    return jsonify({
        "object": "list",
        "data": list(AVAILABLE_MODELS.values())
    })

@app.route('/v1/models/<model_id>', methods=['GET'])
def retrieve_model(model_id):
    """Retrieve a specific model."""
    if model_id in AVAILABLE_MODELS:
        return jsonify(AVAILABLE_MODELS[model_id])
    return jsonify({
        "error": {
            "message": f"The model '{model_id}' does not exist",
            "type": "invalid_request_error",
            "code": "model_not_found"
        }
    }), 404

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "timestamp": time.time()})

@app.route('/v1/engines', methods=['GET'])
def list_engines():
    """Legacy endpoint for listing models (engines)."""
    return list_models()

def run_server(host=HOST, port=PORT, debug=False):
    """Run the mock server."""
    print(f"Starting mock OpenAI compatible server at http://{host}:{port}")
    print(f"Available endpoints:")
    print(f"  POST /v1/chat/completions")
    print(f"  GET  /v1/models")
    print(f"  GET  /v1/models/{{model_id}}")
    print(f"  GET  /health")
    print(f"\nExample usage:")
    print(f"  curl -X POST http://{host}:{port}/v1/chat/completions \\")
    print(f"    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"model\": \"mock-model\", \"messages\": [{{\"role\": \"user\", \"content\": \"Hello!\"}}]}}'")
    app.run(host=host, port=port, debug=debug)

if __name__ == '__main__':
    # Run server in a separate thread for testing
    server_thread = threading.Thread(
        target=run_server,
        kwargs={'host': HOST, 'port': PORT, 'debug': False}
    )
    server_thread.daemon = True
    server_thread.start()

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down server...")

