# NOTE: Scripts/tune/tune.py のコピー (ワーカーの TUNE ビルドが subprocess で実行する)。
# 変更するときは両方を同じ内容に保つこと。
import sys
import os
import re
import traceback
import copy
from dataclasses import dataclass, field
from ParamLib import *

@dataclass
class Block:
    # block種別('set', 'context', 'add)
    type : str = ""

    # block名の後ろに続いていた文字列。
    params : list[str] = field(default_factory=list)

    # blockの中身
    content : list[str] = field(default_factory=list)

# .tuneファイルの1patchを表現する型
@dataclass
class TuneBlock:
    # setblockに書いてあったパラメーター。
    # `file`とか`tune`とか。
    setblock : dict[str,str] = field(default_factory=dict)

    # このcontext_blockに出現したパラメーターの名前。
    # これはパラメーターのsuffix。
    # 💡 `@1`なら`_1`のように置換される。
    params : list[str] = field(default_factory=list)

    # contextブロック
    # context[0] 元の.tuneファイルに書いてあった内容
    # context[1] context[0]から'@123a'のような文字列を除去したもの。
    #            これがソースコードに対する置換対象文字列。
    # context[2] context[0]から、'123@`のような文字列を変数名に置換したもの。
    #            applyコマンドでは、これを現在のパラメーター値で置き換える。
    context_blocks : list[Block] = field(default_factory=list)

    # addブロック(これは複数ありうる)
    add_blocks : list[Block] = field(default_factory=list)


def parse_tune_file(tune_file:str)->list[Block]:
    print(f"parse tune file, path = {tune_file}", end="")

    with open(tune_file, "r", encoding="utf-8") as f:
        filelines = f.readlines()

    # 返し値
    blocks : list[Block] = []

    # 次のブロックを取得する。
    # "#..."から次の"#..."の前の行まで
    lineNo = 0
    def get_next_block()->Block | None:
        nonlocal lineNo, filelines
        lines : list[str] = []
        block_type = ""
        block_params : list[str] = []
        while lineNo < len(filelines):
            line = filelines[lineNo]
            if line.startswith("#"):
                # すでにblock_typeが格納済みであれば、これは次のブロックの開始行だからwhileを抜ける。
                if block_type:
                    break
                # "//" 以降を無視
                line = line.split("//", 1)[0].strip()

                # 空白で分割（空要素を除外）
                parts = line.split()
                if len(parts) >= 2:
                    block_params = parts[1:]
                block_type = parts[0][1:]
            else:
                lines.append(line)
            lineNo += 1

        # ブロック名をすでに設定されてあるなら
        if block_type:
            return Block(block_type, block_params, lines)
        else:
            return None

    while True:
        block = get_next_block()
        # ブロックがなければ終了
        if not block:
            break
        blocks.append(block)

    print(f", {len(blocks)} Blocks.")

    # for block in blocks:
    #     print(block)

    return blocks


def read_tune_file(tune_file:str)->list[TuneBlock]:

    blocks = parse_tune_file(tune_file)
    print(f"read tune file, path = {tune_file}", end="")

    # 返し値
    result : list[TuneBlock] = []

    # 現在の`#set`blockの保存内容
    setblock : dict[str,str] = {}

    current_block = TuneBlock()

    def append_check():
        # "#file"と"#context"とがあるとそこまでのTuneBlockを書き出す。
        nonlocal current_block
        # current_blockがすでに埋まっていれば、書き出す。
        if current_block.context_blocks:
            # setblockの内容をコピー。これはTuneBlockを超えて設定を引き継ぐので…。
            current_block.setblock = dict(setblock)

            # contextブロックの内容
            context = current_block.context_blocks[0]

            # contextから'@123a'のような文字列を除去したもの。これがソースコードに対する置換対象文字列。
            context_with_no_parameters = [re.sub(r"@[0-9A-Za-z]*", "", line) for line in context.content]
            current_block.context_blocks.append(Block(context.type, context.params , context_with_no_parameters))

            result.append(current_block)
            current_block = TuneBlock()

    for block in blocks:
        block_name = block.type
        if block_name.startswith("set"):
            # 新しいセクションかも知れないのでいまあるものを追記する。
            append_check()
            if len(block.params) < 2:
                raise Exception(f"Error : Insufficient parameters in set block, {block}")
            setblock[block.params[0]] = block.params[1]
        elif block_name.startswith("context"):
            # 新しいセクションなのでいまあるものを追記する。
            append_check()

            # if len(block.params) < 1:
            #     raise Exception(f"Error : Insufficient parameter in content block, {block}")
            # 📝 無名contentブロックは、置換用に使うようにした。

            current_block.context_blocks.append(block)
        elif block_name.startswith("add"):
            current_block.add_blocks.append(block)

    append_check()
            
    print(f", {len(result)} TuneBlocks.")
    return result


def parse_content_block(block:Block, prefix:str):
    """
        blockを与えて、`123@234`のような文字列を
        removedのほうに`@`の左側の数値を格納していき、
        params_nameのほうに`@`右側の英数字から作った変数名を格納していく。
        変数名にはprefixをくっつける。
    
        # 置換対象文字列から削除された(元あった)パラメーター
        removed_numbers : list[str] = []

        modified_block : パラメーター名で置き換えたものを返す。
    """

    lines        = block.content
    param_prefix = prefix

    modified_block:list[str] = []
    removed_numbers:list[str] = []
    params_name:list[str] = []

    # 省略されたパラメーター番号(連番)
    param_no = 1

    # "123@234"のように左側の数値を集めてかつ削除する。
    def collect_and_remove(match):
        removed_numbers.append(match.group())
        return '' # 削除
    
    # "@"とその後の英数字を順番に置換。
    # "@"が省略されていれば順番に"@1","@2",...に置換。
    def replace_at(match):
        nonlocal param_no
        suffix = match.group(1) # "@"の後ろの文字。なければ空。
        if not suffix:
            suffix = str(param_no)
            param_no += 1
        param_name = f"{param_prefix}_{suffix}"
        params_name.append(param_name)
        return param_name
    
    for line in lines:
        # 数字(小数含む)を削除しつつ removed_numbers に格納
        modified = re.sub(r'-?\d+(?:\.\d+)?(?=@)', collect_and_remove, line)
        # "@"とその後の英数字を順番に置換
        modified = re.sub(r'@([A-Za-z0-9]*)', replace_at, modified)
        modified_block.append(modified)

    return modified_block, removed_numbers, params_name

def replace_context(filename:str, context:list[str], modified:list[str]):
    """ ファイルのなかのcontextに合致したところをmodifiedに置換する。"""

    path = os.path.join(target_dir, filename)
    # print(path)

    if not os.path.exists(path):
        raise Exception(f"file not found : {path}")

    # context の各文字の間に \s*（空白類0回以上）を挟む正規表現パターンを生成
    context2 = "".join(context)
    # cintext2 から空白・タブ・改行をすべて除去
    context2 = re.sub(r'\s+', '', context2)
    pattern = r'\s*'.join(map(re.escape, context2))

    # これに置き換える。
    replaced = "".join(modified)

    with open(path, 'r', encoding='utf-8') as f:
        filetext = f.read()

    # re.subnを使うと (置換後のテキスト, 置換回数) が返る
    new_text, count = re.subn(pattern, replaced , filetext, flags=re.MULTILINE | re.DOTALL)            

    if count != 1:
        print("target context : ")
        print(context)
        raise Exception(f"Error : replaced count = {count}")

    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_text)

def add_content(filename:str, marker : str, lines: list[str]):
    """
    ソースファイルのmarkerが書いてある行の次の行にlinesを追加する。
    """
    path = os.path.join(target_dir, filename)

    if not os.path.exists(path):
        raise Exception(f"file not found : {path}")

    # ファイルの内容を読み込む
    with open(path, 'r', encoding='utf-8') as f:
        content = f.readlines()

     # 新しい内容を格納するリスト
    new_content = []
    marker_found = False

    for line in content:
        new_content.append(line)
        if marker in line and not marker_found:
            # マーカーが見つかった次の行にlinesを挿入
            new_content.extend(lines)
            marker_found = True

    if not marker_found:
        raise Exception(f"Error : marker not found, marker = {marker} , file path = {filename}")

    # ファイルに書き戻す
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(new_content)


def print_block(block:list[str]):
    """
    blockの画面出力用
    """
    for line in block:
        print(line)


def apply_parameters(tune_file:str , params_file : str, target_dir:str):

    try:
        params = read_parameters(params_file)
    except:
        params : list[Entry] = []

    tune_blocks = read_tune_file(tune_file)

    # 変数名を列挙する。
    for tune_block in tune_blocks:
        content_block = tune_block.context_blocks[0]
        prefix        = content_block.params[0] if content_block.params else ""
        modified_block , removed_numbers, params_name = parse_content_block(content_block , prefix)

        # print("modified block")
        # print(modified_block)

        # contextのコピー。
        context_lines = list(modified_block)

        if prefix:
            # contextをparamの実際の値で置き換えていく。
            for param_name in params_name:
                # このパラメーターの値
                value , type = next(((e.v, e.type) for e in params if e.name == param_name), (None,None))
                if value is None:
                    print_block(content_block.content)
                    raise Exception(f"Error! : {param_name} not found in {params_file}.")

                # print(f"replace : {param_name} -> {value}")

                # パラメーターはfloat表記で持っているので、"int"を要求しているならintに変換する必要がある。
                if type == "int":
                    value = int(value + 0.5) # このときに値を丸める。

                # in-place文字置換           
                context_lines[:] = [line.replace(param_name, str(value)) for line in context_lines]

        else:
            # 無名contextブロック
            # この場合、強制的に次の無名addブロックで置換する
            for add_block in tune_block.add_blocks:
                if len(add_block.params) == 0:
                    context_lines = add_block.content
                    break
                else:
                    raise Exception("置換対象となる無名addブロックが来ていない。")


        # 前者を後者で置換する。
        # (.tuneファイルはWindows由来のパス区切りのことがあるので正規化)
        filename = tune_block.setblock['file'].replace('\\', os.sep)
        print(f"file : {filename}")

        # print("target context")
        # print_block(block.context_lines_no_param)
        # print("final context")
        # print_block(context_lines)

        context  = tune_block.context_blocks[1].content
        replaced = context_lines
        replace_context(filename, context, replaced)

        print(f"Patch applied to {prefix} .. done.")

    print("All patches have been applied successfully.")



def tune_parameters(tune_file:str, params_file : str, target_dir:str):

    print("start tune_parameters()")

    try:
        params = read_parameters(params_file)
    except:
        params : list[Entry] = []

    # いったん、全部使わないパラメーターとして、使っているものだけをFalseにする。
    for param in params:
        param.not_used = True

    tune_blocks = read_tune_file(tune_file)

    def check_params(tune_block:TuneBlock):
        # linesのなかから、変数(`@`)を探して、なければparamに追加。
        block = tune_block.context_blocks[0]
        prefix = block.params[0] if block.params else ""
        _ , removed_numbers, params_name = parse_content_block(block, prefix)

        for param_name, number in zip(params_name, removed_numbers):
            try:
                # print(f"variable {param_name}")
                result = next((p for p in params if p.name == param_name), None)
                if result is None:
                    # 変数がなかったので、paramsに追加。
                    # print("..appended")
                    number = float(number)
                    if number > 0:
                        min_, max_ = 0.0 , number * 2
                    elif number == 0: # 0 になっとる。なんぞこれ。boolと違うか？0,1と扱う。
                        min_, max_ = 0, 1
                    else:
                        min_, max_ = number * 2 , 0.0

                    # stepが1未満だとint化すると同じ値になってしまうので、
                    # 少なくとも1以上でなければならない
                    step  = max((max_ - min_) / 20 , 1)

                    # deltaは固定でいいと思う。
                    # 一回のパラメーターを動かす量は step * delta なので stepのほうで調整がなされる。
                    delta = 0.0020

                    # 整数化したものと値が異なる ⇨ 小数部分がある ⇨ float
                    type = "int" if number == int(number) else "float"

                    params.append(Entry(param_name, type , number, min_, max_ , step, delta,"", False))            

                else:
                    # print(f"..found")
                    # 使っていた。
                    result.not_used = False
            except Exception as e:
                raise Exception(f"{e} , param_name = {param_name}")

    # 変数名を列挙する。
    for tune_block in tune_blocks:
        # content blockで使われている`@`を列挙する。
        check_params(tune_block)

    # これでパラメーターファイルは確定したので、これを書き出す。
    write_parameters(params_file, params)

    # このあと、sourceコードにpatchを当てにいく。

    # block.context_blocks[1]を探して、patchを当てる。
    # あとで
    
    #  add blockをparseして、ソースコードの追加場所を探し、そこに追加する。
    for tune_block in tune_blocks:
        filename      = tune_block.setblock['file'].replace('\\', os.sep)
        content_block = tune_block.context_blocks[0]
        prefix        = content_block.params[0] if content_block.params else ""
        modified_block , _, params_name = parse_content_block(content_block, prefix)

        # print(params_name , modified_block)
        # tune_block.context_blocks[1].content

        # 置換対象文字列
        context = tune_block.context_blocks[1].content

        # add_blocksのなかに無名blockがあるなら、contextは、それで置き換えられる。
        # さもなくば、↑のmodified_blockで置き換える。

        replaced = False
        for add_block in tune_block.add_blocks:
            block_name = add_block.params[0] if len(add_block.params) >= 1 else ""
            
            modified_block2 , _, _ = parse_content_block(add_block, prefix)

            if block_name:
                print(f"add block, prefix = {prefix} , name = {block_name}")
                # block_nameをマーカーとして、その次の行に追加する
                add_content(filename, block_name, modified_block2)
            else:
                # context block名
                prefix = tune_block.context_blocks[0].params[0] if tune_block.context_blocks[0].params else ""
                print(f"replace block, prefix = {prefix}")

                # contextが合致する箇所を探す。
                replace_context(filename, context, modified_block2)
                replaced = True

        if not replaced:
            print(f"modified block, prefix = {prefix}")
            replace_context(filename, context, modified_block)

        # あと、ここで得られた変数を追加する。
        #  "int myValue;"" みたいな文字列と、
        #  "TUNE(SetRange(-100, 100), myValue, SetDefaultRange);"みたいな文字列を構築する。
        tune_params_to_declare : list[str] = []
        tune_params_to_options : list[str] = []
        for param_name in params_name:
            # 同じ名前があるはず..
            p = next((p for p in params if p.name == param_name), None)
            if p is None:
                raise Exception() # この可能性はないはず..
            v = int(p.v) if p.type == 'int' else p.v
            tune_params_to_declare.append(f"{p.type} {p.name} = {v}; {'//' + p.comment if p.comment else ''}\n")
            tune_params_to_options.append(f"TUNE(SetRange({p.min}, {p.max}), {p.name}, SetDefaultRange);\n")

        # print(tune_params_to_declare)
        # print(tune_params_to_options)
        # これらをそれぞれ
        # `#set tune`と`#set declare`で指定されたところに追加する。

        if 'declaration' in tune_block.setblock:
            add_content(filename, tune_block.setblock['declaration'], tune_params_to_declare)
        if 'options' in tune_block.setblock:
            add_content(filename, tune_block.setblock['options']    , tune_params_to_options)

    print("end tune_parameters()")


if __name__ == "__main__":

    # 使い方
    # python tune.py [apply|tune] patch_file target_dir

    args = sys.argv
    target_dir = args[3] if len(args) >= 4 else "source"
    tune_file  = args[2] if len(args) >= 3 else "suisho10.tune"
    command    = args[1] if len(args) >= 2 else ""

    target_dir = args[3] if len(args) >= 4 else "source"
    tune_file  = args[2] if len(args) >= 3 else "param/checkshogi.tune"
    command    = args[1] if len(args) >= 2 else "apply"

    params_file, _ = os.path.splitext(tune_file)
    params_file += ".params"

    print(f"command     : {command}")
    print(f"tune_file   : {tune_file}")
    print(f"params_file : {params_file}")
    print(f"target_dir  : {target_dir}")

    # entries = read_parameters("suisho10.params")
    # write_parameters("suisho10-new.params", entries)

    # "suisho10.tune"   ← チューニング指示ファイル
    # "suisho10.params" ← パラメーターファイル

    try:
        if command == "apply":
            apply_parameters(tune_file, params_file, target_dir)
        elif command == "tune":
            tune_parameters(tune_file, params_file, target_dir)
        else:
            print("Usage : python tune.py [apply|tune] tune_file target_dir")
            # tune_fileとtarget_dirのデフォルト値は、"suisho10.tune", "source"
    except Exception as e:
        error_msg = traceback.format_exc()
        print(f"Exception : {e}")
        print(error_msg)
        # 自動化 (ShogiBench ワーカーの TUNE ビルド等) が失敗を検知できるように
        sys.exit(1)
