package main

import (
	"context"
	"fmt"

	// "fmt"

	"time"

	"github.com/chromedp/chromedp"
	"github.com/chromedp/chromedp/kb"
)

func main() {
	optionsAi := append(
		chromedp.DefaultExecAllocatorOptions[:],
		// chromedp.ProxyServer("45.8.211.64:80"),
		chromedp.Flag("headless", false),
		chromedp.UserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
		chromedp.Flag("enable-automation", false),
		chromedp.Flag("disable-web-security", false),
		chromedp.Flag("disable-web-security", false),
		chromedp.Flag("allow-running-insecure-content", true),
	)
	allocCtxYandex, cancel := chromedp.NewExecAllocator(context.Background(), optionsAi...)
	defer cancel()
	ctx, cancel := chromedp.NewContext(allocCtxYandex)
	defer cancel()
	urlMusic := "https://music.yandex.ru"
	var songName, songAuthors, song string
	err := chromedp.Run(ctx,
		chromedp.Navigate(urlMusic),
		chromedp.WaitVisible(`/html/body/div[1]/div[22]/div/span`),
		chromedp.Click(`/html/body/div[1]/div[22]/div/span`), // крестик
		chromedp.WaitVisible(`/html/body/div[1]/div[14]/div/div/div[3]/a/span/span`),
		chromedp.Click(`/html/body/div[1]/div[14]/div/div/div[3]/a/span/span`), // войти
		chromedp.WaitVisible(`/html/body/div[1]/div[16]/div[2]/div/div/div[2]/div[2]/div[1]/p`),
		chromedp.Click(`/html/body/div[1]/div[16]/div[2]/div/div/div[2]/div[2]/div[1]/p`), // Моя волна
		chromedp.Click(`/html/body/div[1]/div[20]/div[1]/div[4]/div[3]/span`),             // выкл. звук
		chromedp.Text(`/html/body/div[1]/div[20]/div[1]/div[2]/div[5]/div/div/div[1]/div[2]/div/div[1]/a`, &songName),
		chromedp.Text(`/html/body/div[1]/div[20]/div[1]/div[2]/div[5]/div/div/div[1]/div[2]/div/div[2]/span`, &songAuthors),
		chromedp.ActionFunc(func(ctx context.Context) error {
			fmt.Println(songName, songAuthors)
			return nil
		}),
	)
	if err != nil {
		panic(err)
	}

	song = songName + songAuthors
	// song := "/play"
	urlDiscord := `https://discord.com/login`
	allocCtxDiscord, cancel := chromedp.NewExecAllocator(context.Background(), optionsAi...)
	defer cancel()
	ctxDiscord, cancel := chromedp.NewContext(allocCtxDiscord)
	defer cancel()
	err = chromedp.Run(ctxDiscord,
		chromedp.Navigate(urlDiscord),
		chromedp.WaitVisible(`/html/body/div[2]/div[2]/div[1]/div[1]/div/div/div/div/form/div[2]/div/div[1]/div[2]/div[2]/div`),
		chromedp.KeyEvent(`Laminanonono@mail.ru`),
		chromedp.Click(`/html/body/div[2]/div[2]/div[1]/div[1]/div/div/div/div/form/div[2]/div/div[1]/div[2]/div[2]/div`),
		chromedp.KeyEvent(``),
		chromedp.Click(`/html/body/div[2]/div[2]/div[1]/div[1]/div/div/div/div/form/div[2]/div/div[1]/div[2]/button[2]`),
		chromedp.WaitVisible(`.scroller_fea3ef > div:nth-child(3) > div:nth-child(5) > div:nth-child(2) > div:nth-child(1) > div:nth-child(1) > svg:nth-child(1) > foreignObject:nth-child(3) > div:nth-child(1)`), // кастрюля
		chromedp.Click(`.scroller_fea3ef > div:nth-child(3) > div:nth-child(5) > div:nth-child(2) > div:nth-child(1) > div:nth-child(1) > svg:nth-child(1) > foreignObject:nth-child(3) > div:nth-child(1)`),       // кастрюля
		chromedp.WaitVisible(`li.containerDefault_f6f816:nth-child(7) > div:nth-child(1) > div:nth-child(1) > a:nth-child(1) > div:nth-child(1) > div:nth-child(2)`),                                               // ботик
		chromedp.Click(`li.containerDefault_f6f816:nth-child(7) > div:nth-child(1) > div:nth-child(1) > a:nth-child(1) > div:nth-child(1) > div:nth-child(2)`),                                                     // ботик
		chromedp.Sleep(3*time.Second),
		chromedp.KeyEvent(song),
		chromedp.KeyEvent(kb.Enter),
		chromedp.Sleep(10*time.Second),
	)
	if err != nil {
		panic(err)
	}
}
