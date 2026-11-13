"""Pure, versioned teaching decisions. Design sources: REFERENCES.txt.
No model, database or network access; recorded inputs are replayable.
"""
VERSION = 'evidence-policy-v1'


def decide(observation):
    status = observation['status']
    phase = observation['phase']
    kind = observation['kind']
    outcome = observation.get('outcome', 'unknown')
    if phase == 'check':
        return {'action': 'record_check', 'reason': '独立检查只记录验证结果，不提供解题帮助。'}
    if status == 'correct':
        return {'action': 'independent_check', 'reason': '练习完成后用未见检查题收集独立证据。'}
    if kind == 'hint':
        level = observation['level']
        return {'action': ('concept_hint', 'step_hint', 'worked_example')[level - 1],
                'reason': '学生主动请求更明确的帮助；帮助后的答对仅算辅助完成。'}
    if kind == 'reply':
        if outcome == 'slip':
            return {'action': 'self_correct', 'reason': '学生明确描述遗漏或笔误，先让其独立修正数学步骤。'}
        if outcome in ('misconception', 'not_helped'):
            return {'action': 'prerequisite_review', 'reason': '回答显示概念困惑或明确表示帮助无效，换用基础规则解释。'}
        if outcome == 'helped':
            return {'action': 'self_correct', 'reason': '学生报告帮助有效，但仍需提交数学步骤验证。'}
        return {'action': 'clarify', 'reason': '回答证据不足，不能把原因猜测当成已确认误区。'}
    if status == 'unknown':
        return {'action': 'clarify', 'reason': '无法可靠验证作答，先补充可判定步骤。'}
    if observation.get('failures', 0) >= 2:
        return {'action': 'prerequisite_review', 'reason': '重复失败，改用规则解释并要求应用，避免重复同一追问。'}
    return {'action': 'probe', 'reason': '仅凭错误结果不能区分概念误解与遗漏，先询问该步依据。'}
