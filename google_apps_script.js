// ===== Google Apps Script =====
// 把这段代码粘贴到 Google 表格的 Apps 脚本编辑器中
// 路径：扩展程序 > Apps 脚本 > 粘贴代码 > 部署为 Web 应用

function doPost(e) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("评论");
  if (!sheet) {
    sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  }

  var data = JSON.parse(e.postData.contents);

  // 写入一行：帖子ID | 昵称 | 评论内容 | 日期 | 作品标题
  sheet.appendRow([
    data.post_id || "",
    data.nickname || "",
    data.comment || "",
    data.date || "",
    data.title || ""
  ]);

  return ContentService.createTextOutput(
    JSON.stringify({status: "ok"})
  ).setMimeType(ContentService.MimeType.JSON);
}

// 测试用
function doGet(e) {
  return ContentService.createTextOutput("OK - 评论同步接口正常运行");
}
