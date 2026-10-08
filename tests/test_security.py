# copyright by berlonak
# telegram: @Kilax123
"""Test access control independently of optional Telegram library availability."""
from pathlib import Path
import ast
from types import SimpleNamespace


def load_access_predicate():
    source = Path(__file__).resolve().parents[1].joinpath('bot.py').read_text(encoding='utf-8')
    module = ast.parse(source)
    fn = next(x for x in module.body if isinstance(x, ast.FunctionDef) and x.name=='can_use')
    fn.args.args[0].annotation = None
    fn.args.args[1].annotation = None
    fn.returns = None
    result={}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'<can_use>','exec'),result)
    return result['can_use']


def test_only_allowed_private_user_has_access():
    can_use=load_access_predicate()
    settings=SimpleNamespace(allowed_ids=frozenset({123,456}))
    allowed=SimpleNamespace(effective_user=SimpleNamespace(id=123),
                            effective_chat=SimpleNamespace(type='private'))
    other=SimpleNamespace(effective_user=SimpleNamespace(id=999),
                          effective_chat=SimpleNamespace(type='private'))
    group=SimpleNamespace(effective_user=SimpleNamespace(id=123),
                          effective_chat=SimpleNamespace(type='supergroup'))
    assert can_use(allowed,settings) is True
    assert can_use(other,settings) is False
    assert can_use(group,settings) is False


def test_each_telegram_entrypoint_is_decorated():
    source=Path(__file__).resolve().parents[1].joinpath('bot.py').read_text(encoding='utf-8')
    mod=ast.parse(source)
    protected={'start','my_id','uid_cmd','any_text','callbacks'}
    for node in mod.body:
        if isinstance(node,ast.AsyncFunctionDef) and node.name in protected:
            assert any(isinstance(d,ast.Name) and d.id=='access_restricted' for d in node.decorator_list)
            protected.remove(node.name)
    assert not protected
