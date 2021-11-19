isBilibili = window.location.href.indexOf("bilibili.com") > -1;

function getCookie(cname) {
    let name = cname + "=";
    let decodedCookie = decodeURIComponent(document.cookie);
    let ca = decodedCookie.split(';');
    for (let i = 0; i < ca.length; i++) {
        let c = ca[i];
        while (c.charAt(0) === ' ') {
            c = c.substring(1);
        }
        if (c.indexOf(name) === 0) {
            return c.substring(name.length, c.length);
        }
    }
    return "";
}

document.addEventListener("DOMContentLoaded", function () {
    if (!isBilibili) {
        const all_bilibili_dom = document.getElementsByClassName("bilibili");
        Array.from(all_bilibili_dom).forEach((element) => {
            element.remove();
        })
    }
    document.querySelector("body").addEventListener('click', function (e) {
        const anchor = e.target.closest('a');
        if (anchor !== null) {
            if (!anchor.hasAttribute("data-href")) return;
            if (isBilibili) {
                fetch(anchor.dataset.href)
                    .then(resp => resp.text())
                    .then(data => {
                        document.open();
                        document.write(data);
                        document.close()
                    });
            } else {
                window.location.href = anchor.dataset.href;
            }
        }
    }, false);
});
