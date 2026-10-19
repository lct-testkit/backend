<#ftl output_format="plainText">
<#-- Общий макет текстовой части письма: заголовок, тело, ссылка, пояснение, подвал. -->
<#macro emailLayout heading="" link="" label="" note="">
${heading}

<#nested>
<#if link?has_content>

${label}: ${link}
</#if>
<#if note?has_content>

${note}
</#if>

--
${msg("footerAuto")}
</#macro>
