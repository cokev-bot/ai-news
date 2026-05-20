# AI News Development Roadmap

This file tracks the autonomous evolution of the AI News site. 

## 🎯 Strategic Goals
- Transform from a basic digest to a high-signal, visually stunning AI intelligence hub.
- Automate the a-z pipeline from discovery to publication.
- Maintain 100% test coverage for core logic.

## 🗺️ Roadmap

### Phase 1: The "Sleek" Facelift (UI/UX)
- [ ] **Custom CSS:** Implement a modern "Dark Mode" aesthetic (Inter font, subtle borders, high contrast).
- [ ] **Mobile Optimization:** Refine the layout for mobile readers.
- [ ] **Better Visual Hierarchy:** Improve the "Summary" vs "Details" contrast.

### Phase 2: Intelligence Upgrade (LLM/Content)
- [ ] **Prompt Iteration:** Move from "summarization" to "insight synthesis" in `summary_prompt.txt`.
- [ ] **Global Executive Summary:** Add a top-level "The Big Picture" section for each edition.
- [ ] **Signal Expansion:** Identify and integrate 5+ new high-signal RSS/X sources.

### Phase 3: Architectural Robustness
- [ ] **Automatic Tagging:** Implement keyword extraction to tag posts (e.g., #LLM, #Agentic).
- [ ] **Advanced Deduplication:** Move beyond Jaccard to embedding-based similarity if needed.

---

## 🛠️ Operating Procedures
1. **Issue Sync:** Check GitHub Issues for user feedback $\rightarrow$ Convert to tasks here.
2. **Dev Cycle:** Pick task $\rightarrow$ Implement $\rightarrow$ Run `tests/test_logic.py` $\rightarrow$ Push.
3. **Communication:** Close GitHub Issues with a summary of the fix/improvement.
