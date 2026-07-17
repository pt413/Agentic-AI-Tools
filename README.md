<div align="center">

# 🤖 Agentic AI Tools

### A collection of production-inspired Agentic AI workflows built from scratch

Build real-world AI systems using **LangGraph**, **RAG**, **Planning**, **Tool Calling**, **Preflight Validation**, **State Management**, **Synthesizers**, and more.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-Agent_Workflows-green)
![FastAPI](https://img.shields.io/badge/FastAPI-Backend-success)
![License](https://img.shields.io/badge/License-MIT-orange)

</div>

---

# Overview

This repository demonstrates how modern **Agentic AI systems** are actually built.

Instead of isolated examples, each project combines multiple components into complete AI workflows similar to those used in production systems.

Examples include:

- Retrieval-Augmented Generation (RAG)
- Multi-step Planning
- LangGraph State Machines
- Tool Orchestration
- Preflight Validation
- LLM Routing
- Context Management
- Response Synthesis
- Memory
- Vector Search
- Structured Outputs

The goal is to help developers understand **how the entire pipeline works together**, not just individual libraries.

---

# Architecture

```

User Query
│
▼
Planner
│
├──────────────┐
│              │
▼              ▼
Preflight   Direct Answer
│
▼
Retriever (RAG)
│
▼
Context Builder
│
▼
LLM
│
▼
Synthesizer
│
▼
Final Response

```

Depending on the workflow, additional nodes may include:

- Tool Executor
- Memory
- Re-ranking
- Query Rewriting
- Guardrails
- Human-in-the-loop

---

# Features

✅ Retrieval-Augmented Generation (RAG)

✅ LangGraph Workflows

✅ Planner-Based Execution

✅ Preflight Validation

✅ Context Engineering

✅ Tool Calling

✅ Conditional Routing

✅ State Management

✅ Multi-Step Reasoning

✅ Response Synthesis

✅ Production-Oriented Folder Structure

---

# Workflow Example

```

Question

↓

Preflight Validation

↓

Planner

↓

Should I use tools?

↓

Yes

↓

Which Tools?

↓

Retrieve Documents

↓

Build Context

↓

LLM Generation

↓

Synthesizer

↓

Final Answer

```

---

# Technologies Used

- FastApi
- LangGraph
- OpenAI / Ollama
- pgvector / Pinecone / Chroma
- AI Agents
- Pydantic
- PostgreSQL / Neon DB
- Retrieval Augmented Generation
- LLM Orchestration
- Vector RAG / Graph RAG
- Hybrid Search / Vector Search
- Code Reranker

---

# Why This Repository?

Many tutorials show only a single concept.

This repository focuses on combining multiple concepts into complete AI workflows.

For example, instead of showing only RAG, a workflow may include:

Planner
→ Preflight Validation
→ Query Rewriting
→ Retrieval
→ LLM
→ Synthesizer

This better reflects how production AI systems are designed.

---

# Current Workflows

| Workflow | Status |
|-----------|--------|
| Advanced RAG | ✅ |
| LangGraph Planner | ✅ |
| Preflight Validation | ✅ |
| LLM Router | ✅ |
| Response Synthesizer | ✅ |
| Multi-step Agent | 🚧 |
| Memory | 🚧 |
| Reflection | 🚧 |
| Human Feedback | 🚧 |

---

# Getting Started

Clone the repository

```bash
git clone https://github.com/<username>/Agentic-AI-Tools.git

cd Agentic-AI-Tools
```

Install dependencies

```bash
pip install -r requirements.txt
```

Configure environment

```bash
OPENAI_API_KEY=your_key
```

Run a workflow

```bash
python main.py
```

---

# Learning Path

If you're new to Agentic AI, follow this order:

1. Basic RAG
2. LangGraph Fundamentals
3. Planner
4. Tool Calling
5. Preflight Validation
6. Context Engineering
7. State Management
8. Synthesizer
9. Multi-Agent Workflows

---

# Inspiration

This repository is inspired by production AI architectures used across modern LLM applications, rather than toy chatbot examples.

The implementation emphasizes clarity, modularity, and extensibility.

---

# Contributing

Contributions are welcome.

If you'd like to add new workflows, improve documentation, or fix issues, feel free to open a Pull Request.

---

# License

MIT License

---

<div align="center">

⭐ If you found this repository useful, consider starring it.

Happy Building!

</div>
