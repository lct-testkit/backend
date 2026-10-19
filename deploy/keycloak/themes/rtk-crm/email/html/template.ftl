<#-- Общий макет писем Keycloak для RTK School CRM: 600px, таблицы и инлайн-стили, токены rt-ui rtk_default
     (светлая и тёмная темы), как в письмах backend. Параметры:
       heading   заголовок письма
       preheader строка предпросмотра в списке писем (в письме скрыта)
       link      ссылка кнопки (без неё кнопки и запасной ссылки нет)
       label     подпись кнопки
       note      пояснение под кнопкой
     Логотип берётся из ресурсов самой темы (см. logoUrl ниже); не загрузилась картинка — рядом остаётся текст «RTK School». -->
<#macro emailLayout heading="" preheader="" link="" label="" note="">
<#-- Логотип отдаёт сам Keycloak: url.resourcesUrl = https://хост/auth/resources/<версия>/email/rtk-crm (картинка лежит в resources/img/logo.png темы). -->
<#assign logoUrl = (url.resourcesUrl)!"">
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>${heading}</title>
<style>
  :root { color-scheme: light dark; supported-color-schemes: light dark; }
  @media (prefers-color-scheme: dark) {
    .bg-page { background-color: #151d2c !important; }
    .bg-card { background-color: #1b2332 !important; }
    .bg-soft { background-color: #212938 !important; }
    .t-main  { color: #e8e8ee !important; }
    .t-muted { color: #9da1ac !important; }
    .line    { border-color: #323a49 !important; }
    .btn     { background-color: #ffa789 !important; }
    .btn-a   { color: #591c06 !important; }
    .a-link  { color: #ffa789 !important; }
  }
  @media only screen and (max-width: 620px) {
    .wrap { width: 100% !important; }
    .pad  { padding-left: 20px !important; padding-right: 20px !important; }
    .h1   { font-size: 22px !important; line-height: 28px !important; }
  }
</style>
</head>
<body class="bg-page" style="margin:0;padding:0;background-color:#f4f4f5;-webkit-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;mso-hide:all;">${preheader}&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;&#8199;&#847;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bg-page" style="background-color:#f4f4f5;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" class="wrap" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;">
    <tr><td class="pad" style="padding:0 32px 16px 32px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
        <#if logoUrl?has_content><td style="vertical-align:middle;"><img src="${logoUrl}/img/logo.png" width="40" height="40" alt="" style="display:block;border:0;outline:none;"></td></#if>
        <td style="vertical-align:middle;<#if logoUrl?has_content>padding-left:12px;</#if>font-family:Arial,Helvetica,sans-serif;">
          <div class="t-main" style="font-size:16px;line-height:20px;font-weight:bold;color:#101828;">${msg("brandName")}</div>
          <div class="t-muted" style="font-size:12px;line-height:16px;color:#6d7686;">${msg("brandTagline")}</div>
        </td>
      </tr></table>
    </td></tr>
    <tr><td class="bg-card line" style="background-color:#ffffff;border:1px solid #e8e8ee;border-radius:12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr><td style="height:4px;line-height:4px;font-size:0;background-color:#ff4f12;border-radius:12px 12px 0 0;">&nbsp;</td></tr>
        <tr><td class="pad" style="padding:32px 32px 8px 32px;font-family:Arial,Helvetica,sans-serif;">
          <h1 class="h1 t-main" style="margin:0;font-size:24px;line-height:30px;font-weight:bold;color:#101828;">${heading}</h1>
        </td></tr>
        <tr><td class="pad t-main" style="padding:8px 32px 8px 32px;font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:24px;color:#101828;">
          <#nested>
        </td></tr>
        <#if link?has_content>
        <tr><td class="pad" style="padding:16px 32px 16px 32px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
            <td class="btn" align="center" bgcolor="#d9430f" style="background-color:#d9430f;border-radius:8px;">
              <a class="btn-a" href="${link}" target="_blank" style="display:inline-block;padding:14px 28px;font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:20px;font-weight:bold;color:#ffffff;text-decoration:none;border-radius:8px;">${label}</a>
            </td>
          </tr></table>
          <div class="t-muted" style="padding-top:12px;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;color:#6d7686;">${msg("fallbackLinkLead")}<br><a class="a-link" href="${link}" style="color:#d9430f;word-break:break-all;">${link}</a></div>
        </td></tr>
        </#if>
        <tr><td class="pad t-muted" style="padding:8px 32px 32px 32px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:20px;color:#6d7686;">
          ${note}
        </td></tr>
      </table>
    </td></tr>
    <tr><td class="pad t-muted" style="padding:20px 32px 0 32px;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;color:#6d7686;text-align:center;">
      ${msg("footerAuto")}<br>
      ${msg("footerReason")}
    </td></tr>
  </table>
</td></tr>
</table>
</body>
</html>
</#macro>
