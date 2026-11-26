// Insert the HTML brief template into a reply without sending it.
"use strict";

window.ConsoleHtmlBrief = (() => {
  const TEMPLATE = `このスレッドの直近のやり取りを、あとで読み返せるHTMLにまとめてください。

範囲：［直近の話題すべて／特定の話題だけ］
読み手：［自分の振り返り用／チームに共有する］
保存先：Ebi Workspace の「{{project}}」の html/ フォルダに YYYYMMDD_件名.html（プロジェクトが分からなければ候補を挙げて聞く）

作り方：/home/a_suto/services/html-brief/create-html/SKILL.md を読んで従う。ページは scripts/new-page.mjs から作り、scripts/check-html.mjs が OK になるまで直す。
- 会話に出ていない事実は足さない。決まったこと・決まっていないこと・次にやることを分けて書く
- JavaScript は使わない。html共有くんを通さないので、表のスマホ向け折りたたみはページのCSSに入れ、color-scheme: light を指定する

仕上げ：
- 同じフォルダの _index.md の先頭に1行足す（日付・件名・HTMLへのリンク・このスレッドへのリンク /#focus/t<スレッドID>）
- HTMLを添付ファイルとして送る
`;

  function insert(box, workspacePath) {
    const template = TEMPLATE.replace("{{project}}", workspacePath || "［プロジェクト］");
    box.value = box.value.trim() ? `${box.value.replace(/\s+$/, "")}\n\n${template}` : template;
    box.dispatchEvent(new Event("input", { bubbles: true }));
    box.focus();
    const blank = box.value.indexOf("［");
    if (blank < 0) return;
    const close = box.value.indexOf("］", blank);
    box.setSelectionRange(blank, close >= 0 ? close + 1 : blank);
  }

  function button(box, workspacePath) {
    const label = "HTMLにまとめるテンプレートを入れる（送信はしません）";
    return h("button", { class: "btn html-brief", type: "button", title: label, "aria-label": label,
      onclick: (e) => { e.preventDefault(); insert(typeof box === "function" ? box() : box,
        typeof workspacePath === "function" ? workspacePath() : workspacePath); }, text: "📄" });
  }

  return { insert, button };
})();
